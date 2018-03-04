import argparse
import datetime
import pickle
import requests
import html
import time
import os

from requests.cookies import RequestsCookieJar
from hsize import human2bytes


def clean_string(string):
    string = string.replace('&#160;', ' ')
    string = html.unescape(string)
    return string


class CoxInternetUsage(object):
    def __init__(self, proxy_config=None, username='', password=''):
        self._cookie_jar = None
        self._cookie_file = 'cox-status.bin'
        self._session = None

        # todo: load from configuration
        self._username = username
        self._password = password

        self._proxy_config = proxy_config
        self._ssl_verify = (self._proxy_config is None)

        self._create_session()

    def _create_session(self, restore_cookies=True):
        if restore_cookies:
            try:
                with open(self._cookie_file, 'rb') as f:
                    self._cookie_jar = pickle.load(f)
                print('Loaded saved login session')
            except Exception:
                self._cookie_jar = RequestsCookieJar()
        else:
            self._cookie_jar = RequestsCookieJar()

        self._session = requests.Session()
        self._session.cookies = self._cookie_jar
        self._session.proxies = self._proxy_config or {}
        self._session.verify = self._ssl_verify
        self._session.headers['User-Agent'] = \
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' +\
            'Chrome/63.0.3239.84 Safari/537.36'

    def _do_login(self):
        # clear all cookies to redo login
        self._cookie_jar.clear()

        print('Trying to log in')

        form_data = (
            ('emaildomain', '@cox.net'),
            ('username', self._username),
            ('password', self._password),
            ('signin-submit', 'Sign In'),
            ('rememberme', 'on'),
            ('targetFN', 'COX.net'),
            ('onsuccess', 'https%3A%2F%2Fwww.cox.com%2Fresaccount%2Fhome.cox'),
            ('onfailure',
             'http://www.cox.com/resaccount/orangecounty/sign-in.cox?' +
             'onsuccess=https%3A%2F%2Fwww.cox.com%2Fresaccount%2Fhome.cox')
        )

        success = False

        try:
            self._session.post('https://idm.east.cox.net/idm/coxnetlogin',
                               data=form_data)
        except requests.HTTPError as e:
            print(e.message)
            success = False

        # find the login cookie to see if we're logged in
        loggedin_cookie = self._cookie_jar.get('SM_LOGGEDIN', None, domain='.cox.net')
        if loggedin_cookie is not None:
            success = True
            print('Logged in!')

            # access the internet endpoint to create session id
            self._session.get('https://www.cox.com/internet/mydatausage.cox')
        else:
            print('Did\'t find cookie.')
            print(self._cookie_jar)

        # save cookies
        with open(self._cookie_file, 'wb') as f:
            pickle.dump(self._cookie_jar, f)

        return success

    def _get_with_auth(self, url):
        response = self._session.get(url)
        if response.status_code == 401:
            logged_in = self._do_login()
            if not logged_in:
                print('Failed to log in')
                response.raise_for_status()

            response = self._session.get(url)

        response.raise_for_status()
        return response

    def get_usage_data(self):
        timestamp = int(time.time()) * 1000 * 1000  # microseconds

        data_url = 'https://www.cox.com/internet/ajaxDataUsageJSON.ajax?usagePeriodType=daily&_={0}'.format(timestamp)
        response = self._get_with_auth(data_url)
        response.raise_for_status()

        return response.json()

    def process_data(self, server, data):
        modem_details = data['modemDetails'][0]

        # check for errors
        error_daily = modem_details['errorDaily']
        if error_daily is not None:
            error_code = error_daily['errorCode']
            error_message = error_daily['errorMessage']

            # print error and reset the session
            print('Error {0} - {1}'.format(error_code, error_message))
            print('Clearing Session, will try again in an hour...')
            self._create_session(False)
            return

        data_details = modem_details['dataUsed']

        data_used = human2bytes(clean_string(data_details['totalDataUsed']))
        data_total = human2bytes(clean_string(modem_details['dataPlan']))

        records = []
        gigabytes = 1024 * 1024 * 1024

        data_used = float(data_used) / gigabytes
        data_total = float(data_total) / gigabytes
        print('monthly data used: {0} GB'.format(data_used))

        records.append('current_monthly_usage current={0},remaining={1}'
                       .format(data_used, data_total - data_used))
        records.append('current_monthly_total value={0}'.format(data_total))

        # parse the service period
        service_period = modem_details['servicePeriod']
        service_start, service_end = service_period.split('-')

        service_start = datetime.datetime.strptime(service_start, '%m/%d/%y')
        service_end = datetime.datetime.strptime(service_end, '%m/%d/%y')

        today = datetime.datetime.now()
        time_left = service_end - today
        print('{0} days remaining on current cycle'.format(time_left.days))

        time_into = today - service_start
        records.append('cycle_days remaining={0},current={1}'.format(time_left.days, time_into.days))

        # find today's usage
        daily_usage = data_details['daily']

        # gather up all of the days up until today
        for i, daily_data in enumerate(daily_usage):
            date_point = service_start + datetime.timedelta(days=i)
            if date_point > today:
                break

            data_measurement = int(daily_data['data'])
            records.append('daily_usage,date={0} value={1}'.format(date_point.date(), data_measurement))

        last_update_date = modem_details['lastUpdatedDate']
        last_update_date = datetime.datetime.strptime(last_update_date, '%m/%d/%y')
        last_update_date = last_update_date.strftime('%m/%d/%Y')
        records.append('last_update value="{0}"'.format(last_update_date))

        # loop over the data and bind each measurement to a date string
        submit_data = '\n'.join(records)

        post_to_influxdb(server, submit_data)
        print('Server updated successfully')


def post_to_influxdb(server, data):
    requests.post(server, data=data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Send cox usage statistics to influxdb')
    parser.add_argument('--username', help='cox account username',
                        default=os.environ.get('COX_STATUS_USERNAME', ''))
    parser.add_argument('--password', help='cox account password',
                        default=os.environ.get('COX_STATUS_PASSWORD', ''))
    parser.add_argument('--influxdb', help='url to post data to',
                        default=os.environ.get('COX_STATUS_INFLUXDB', ''))

    args = parser.parse_args()

    if len(args.username) == 0 or len(args.password) == 0:
        raise RuntimeError('Missing username and/or password')

    if len(args.influxdb) == 0:
        raise RuntimeError('Missing influxdb server')

    fetcher = CoxInternetUsage(username=args.username,
                               password=args.password)

    while True:
        try:
            print('Fetching usage from cox.com:')
            data = fetcher.get_usage_data()
            fetcher.process_data(args.influxdb, data)
            # sleep for an hour
            time.sleep(60 * 60)
        except Exception as e:
            print('An error occurred: {0}'.format(e))
            # sleep for 5 minutes
            time.sleep(5 * 60)
