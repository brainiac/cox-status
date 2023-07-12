import argparse
import datetime
import pickle
import requests
import time
import os
import re
import json
import logging
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from requests.cookies import RequestsCookieJar
from hsize import human2bytes


def remove_comments(js_code):
    pattern = r"/\*[\s\S]*?\*/"
    # Matches /* ... */ comments

    cleaned_code = re.sub(pattern, "", js_code, flags=re.MULTILINE)
    return cleaned_code


class CoxInternetUsage:
    username: str
    password: str

    client: InfluxDBClient
    influxdb_bucket: str

    ssl_verify: bool
    cookie_jar: RequestsCookieJar | None
    cookie_file: str
    sessin: requests.Session

    def __init__(self, ssl_verify: bool = True,
                 username: str = '', password: str = '',
                 influxdb: str = '', bucket: str = '', token: str = '', org: str = ''):
        self.cookie_jar = None
        self.cookie_file = 'cox-status.bin'
        self.session = None
        self.ssl_verify = ssl_verify

        # todo: load from configuration
        self.username = username
        self.password = password

        self.client = InfluxDBClient(url=influxdb, org=org, token=token)
        self.influxdb_bucket = bucket

        self._create_session()

    def _create_session(self, restore_cookies=True):
        if restore_cookies:
            try:
                with open(self.cookie_file, 'rb') as f:
                    self.cookie_jar = pickle.load(f)
                print('Loaded saved login session')
            except Exception:
                self.cookie_jar = RequestsCookieJar()
        else:
            self.cookie_jar = RequestsCookieJar()

        self.session = requests.Session()
        self.session.cookies = self.cookie_jar
        self.session.verify = self.ssl_verify
        self.session.headers['User-Agent'] = \
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' +\
            'Chrome/63.0.3239.84 Safari/537.36'

    def _do_login(self):
        # clear all cookies to redo login
        self.cookie_jar.clear()
        print('Trying to log in')

        json_data = dict(username=self.username, password=self.password)

        try:
            login_js: str = self.session.get('https://www.cox.com/content/dam/cox/okta/okta-login-v7.js').text
            login_js = remove_comments(login_js)

            keys = ['client_id', 'scope', 'base_url', 'issuer']
            results = {}

            for key in keys:
                regex = re.compile(rf"^\s*var {key.upper()} = ['\"](.*)['\"];$", re.MULTILINE)
                match = re.search(regex, login_js)
                if match:
                    results[key] = match.group(1)

            auth_response = self.session.post('https://login.cox.com/api/v1/authn', json=json_data).json()
            session_token = auth_response['sessionToken']

            uri = f'{results["issuer"]}/v1/authorize?client_id={results["client_id"]}' \
                  f'&nonce=htM9v12ncjOc6XGMLjpPwxUE375s9RWOJRidGvviFo9uy1R1sV2H9natdLMYPEQ4' \
                  f'&redirect_uri=https%3A%2F%2Fwww.cox.com%2Fauthres%2Fcode' \
                  f'&response_type=code' \
                  f'&sessionToken={session_token}' \
                  f'&state=abc123' \
                  f'&scope=openid%20internal%20email'
            response = self.session.get(uri)
            response.raise_for_status()

        except requests.HTTPError as e:
            logging.exception(e)
            return False

        # find the login cookie to see if we're logged in
        loggedin_cookie = self.cookie_jar.get('_cidt', None, domain='.cox.com')
        if loggedin_cookie is not None:
            success = True
            print('Logged in!')
        else:
            print('Did\'t find session cookie!')
            return False

        # save cookies
        with open(self.cookie_file, 'wb') as f:
            pickle.dump(self.cookie_jar, f)

        return success

    def _get_with_auth(self, url) -> requests.Response:
        response = self.session.get(url)
        if response.status_code == 401 or 'Sign In to Your Cox Account' in response.text:
            logged_in = self._do_login()
            if not logged_in:
                print('Failed to log in')
                response.raise_for_status()

            response = self.session.get(url)

        response.raise_for_status()
        return response

    def get_usage_data(self, which: str) -> tuple[dict, dict]:
        timestamp = int(time.time()) * 1000  # milliseconds

        # https://www.cox.com/internettools/data-usage.html/graph/current-daily/1?_=1689055587051
        # https://www.cox.com/internettools/data-usage.html/graph/monthly/1?_=1689055626095
        # https://www.cox.com/internettools/data-usage.html/graph/past-daily/1?_=1689055626096

        data_url = f'https://www.cox.com/internettools/data-usage.html/graph/{which}/1?_={timestamp}'
        response1 = self._get_with_auth(data_url)
        response1.raise_for_status()
        data = response1.json()

        summary_url = f'https://www.cox.com/internettools/data-usage.html'
        response2 = self._get_with_auth(summary_url)
        response2.raise_for_status()

        summary_text = response2.text
        summary_data = {}
        match = re.search(r"data-usage-url='(.*)'", summary_text, re.MULTILINE)
        if match:
            summary_data = json.loads(match.group(1))

        return data, summary_data

    def publish_data(self, records: list[Point]):
        write_api = self.client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=self.influxdb_bucket, record=records)
        logging.info(f"Wrote {len(records)} records to influxdb")

    def process_data(self, data: dict, summary: dict) -> list[Point]:
        error_code = None
        error_message = None

        # check for errors
        if data['errorFlag']:
            error_code = data['error']['errorCode']
            error_message = data['error']['errorMessage']

        elif summary['errorFlag']:
            error_code = summary['error']['errorCode']
            error_message = summary['error']['errorMessage']

        if error_code is not None and error_message is not None:
            # print error and reset the session
            print(f'Error {error_code} - {error_message}')
            print('Clearing Session, will try again in an hour...')
            self._create_session(False)
            return []

        gigabytes = 1024 * 1024 * 1024

        today = datetime.datetime.now()
        current_day = today.day
        current_month: int = today.month
        current_year: int = today.year

        summary_data = summary['modemDetails'][0]
        percent_used = int(summary_data['percentageDataUsed']) / 100
        data_used = human2bytes(summary_data['totalDataUsed'])
        data_total = human2bytes(summary_data['dataPlan'])
        records: list[Point] = []

        data_used = float(data_used) / gigabytes
        data_total = float(data_total) / gigabytes
        print(f'monthly data used: {data_used} GB')

        # parse the service period
        service_period = summary_data['usageCycle']
        service_start_str: str
        service_end_str: str
        service_start_str, service_end_str = service_period.split('-')

        service_start = datetime.datetime.strptime(service_start_str.strip(), '%B %d')
        service_end = datetime.datetime.strptime(service_end_str.strip(), '%B %d')

        service_start = service_start.replace(year=current_year)
        service_end = service_end.replace(year=current_year)

        if service_start.month == 12 and current_month == 1:
            service_start = service_start.replace(year=current_year - 1)
        elif service_end.month == 1 and current_month == 12:
            service_end = service_end.replace(year=current_year + 1)

        time_left = service_end - today
        print(f'{time_left.days} days remaining on current cycle')

        records.append(Point('current_monthly_usage')
                       .field('service_period', service_period)
                       .field('current', data_used)
                       .field('remaining', data_total - data_used)
                       .field('total', data_total)
                       .field('percent_used', percent_used))
        records.append(Point('current_monthly_total')
                       .field('service_period', service_period)
                       .field('value', data_total))

        time_into = today - service_start
        records.append(Point('cycle_days').field('remaining', time_left.days).field('current', time_into.days))

        # find today's usage
        usage_data = data['modemGraphDetails'][0]
        for data_node in usage_data['graphData']:
            data_date: str = data_node['text']   # 12/1
            data_data: str = data_node['data']   # 23

            data_month, data_day = [int(x) for x in data_date.split('/')]
            data_bytes = int(data_data)
            data_year = current_year

            if data_day == current_day and data_month == current_month:
                break

            if current_month == 12 and data_month == 1:
                data_year = current_year + 1
            elif current_month == 1 and data_month == 12:
                data_year = current_year - 1

            data_datetime = datetime.datetime(year=data_year, month=data_month, day=data_day)
            records.append(Point('daily_usage').time(data_datetime).field('value', data_bytes))

        last_update_str = summary_data['usageDate']
        match = re.match('Usage as of (.*)', last_update_str)
        if match:
            last_update_date_str = match.group(1)
            last_update_date = datetime.datetime.strptime(last_update_date_str, '%B %d')

            last_update_date = last_update_date.replace(year=current_year)
            if last_update_date.month == 12 and current_month == 1:
                last_update_date = last_update_date.replace(year=current_year - 1)
            elif last_update_date.month == 1 and current_month == 12:
                last_update_date = last_update_date.replace(year=current_year + 1)

            records.append(Point('last_update').field('value', last_update_date.strftime('%m/%d/%Y')))

        return records

def post_to_influxdb(server, data):
    result = requests.post(server, data=data)
    pass


def main():
    parser = argparse.ArgumentParser(description='Send cox usage statistics to influxdb')
    parser.add_argument('--username', help='cox account username',
                        default=os.environ.get('COX_STATUS_USERNAME', ''))
    parser.add_argument('--password', help='cox account password',
                        default=os.environ.get('COX_STATUS_PASSWORD', ''))
    parser.add_argument('--influxdb', help='influxdb server',
                        default=os.environ.get('COX_STATUS_INFLUXDB_SERVER', ''))
    parser.add_argument('--bucket', help='influxdb bucket',
                        default=os.environ.get('COX_STATUS_INFLUXDB_BUCKET', 'coxusage'))
    parser.add_argument('--token', help='influxdb token',
                        default=os.environ.get('COX_STATUS_INFLUXDB_TOKEN', ''))
    parser.add_argument('--influxdb_org', help='influxdb org',
                        default=os.environ.get('COX_STATUS_INFLUXDB_ORG', ''))

    args = parser.parse_args()

    if len(args.username) == 0 or len(args.password) == 0:
        raise RuntimeError('Missing username and/or password')

    if len(args.influxdb) == 0:
        raise RuntimeError('Missing influxdb server')

    fetcher = CoxInternetUsage(username=args.username, password=args.password,
                               influxdb=args.influxdb, bucket=args.bucket,
                               token=args.token, org=args.influxdb_org)

    while True:
        try:
            print('Fetching usage from cox.com:')
            for which in ['current-daily']:
                data, summary = fetcher.get_usage_data(which)
                records = fetcher.process_data(data, summary)
                fetcher.publish_data(records)
            # sleep for an hour
            time.sleep(60 * 60)
        except Exception as e:
            print(f'An error occurred: {e}')
            # sleep for 5 minutes
            time.sleep(5 * 60)


if __name__ == '__main__':
    main()
