import os
import sys
import datetime
import io
import json
import requests
import shutil
import time
import uuid
import jwt
import gzip
import csv
import argparse
import pandas as pd
from pathlib import Path
from typing import Optional, Any, List, Tuple, Dict, Set, Union, Literal
from datetime import datetime, timedelta, timezone

from oauthlib.oauth2 import BackendApplicationClient
from requests_oauthlib import OAuth2Session
from requests.exceptions import HTTPError
from urllib.parse import urljoin
from urllib3.util.retry import Retry

## user package
import read_json as rj
import json_writer as jw
from create_logger import get_logger
from databaselib import exec_sql, execmany_sql
import common_sql_functions as dbm
from createPayloadJson import trigger_payload
import copy_output_file as cp
import get_business_date as bd

## Initialize logger to log on file
logger = get_logger()
EXTRACT_COMPLETE_MSG = "security and pricing extract completed"

####################################################
##      get expiration for the certificate        ##
####################################################
def check_credential_expiration(expiry_date: int, valid_check_days: int) -> bool:
    """
    Check the expiration status of credentials based on the provided expiry date.

    Args:
        expiry_date (int): The expiration date of the credentials in milliseconds since the epoch.
        valid_check_days (int): Number of days to throw the credential expiry alert.

    return boolean:

    Logs:
        - Error: If the credentials have expired.
        - Warning: If the credentials are expiring within 30 days.
        - Info: If the credentials are valid and not expiring within 30 days.
    """
    EXPIRES_IN = datetime.fromtimestamp(expiry_date / 1000, TIMEZONE) - datetime.now(TIMEZONE)
    if EXPIRES_IN.days < 0:
        logger.error(f"{logger_prefix} Credentials expired {EXPIRES_IN.days} days ago")
        return False
    elif EXPIRES_IN < timedelta(days=valid_check_days):
        logger.warning(f"{logger_prefix} Credentials expiring in {EXPIRES_IN}")
    else:
        logger.info(f"{logger_prefix} Credentials expiring in {EXPIRES_IN}")
    return True

def add_busdate_column(output_csv_file: str, record_delimiter: str) -> None:
    logger.info(f"{logger_prefix} Adding business date column for file : {output_csv_file}")

    try:
        if not os.path.exists(output_csv_file):
            logger.error(f"{logger_prefix} File {output_csv_file} does not exist...")
            raise FileNotFoundError(f"File {output_csv_file} does not exist...")

        # Read the CSV file
        df = pd.read_csv(output_csv_file, sep=record_delimiter)

        # Add a new column
        df.insert(0, 'BUSINESS_DATE', str(business_date[1]))

        df.to_csv(output_csv_file, sep=record_delimiter, index=False)
    except FileNotFoundError as e:
        logger.error(f"{logger_prefix} File not found : {e}")
        raise FileNotFoundError(f"File not found : {e} during add_busdate_column.")
    except Exception as e:
        logger.error(f"{logger_prefix} An unexpected error occurred while adding business date column : {e}")
        raise RuntimeError(f"An unexpected error occurred while adding business date column : {e}")

def _build_output_file_name(security_category: str) -> str | None:
    """
    Build the output file name by substituting date placeholders from global output params.
    Returns the resolved filename string, or None if output_param_filename or output_param_datefmt is not set.
    """
    return (
        str(output_param_filename)
        .replace('{securityCategory}', security_category)
        .replace('{currdate}', bd.convert_to_date_format(business_date[1], output_param_datefmt))
        .replace('{prevdate}', bd.convert_to_date_format(business_date[0], output_param_datefmt))
        .replace('{nextdate}', bd.convert_to_date_format(business_date[2], output_param_datefmt))
        if output_param_filename and output_param_datefmt else None
    )


def _resolve_security_master_json(file_dict: dict, output_json_file_name: str, retried_securities: list) -> str:
    """
    Resolve the final JSON file path for the 'securityMaster' security category.

    - If file_dict has 2 entries with is_retry values {'N','Y'}: merges both files,
      filtering out retried_securities from the 'N' file, and writes the merged result
      to output_json_file_name.
    - If file_dict has 1 entry: uses that single file directly as output_json_file_name.
    - Otherwise: raises ValueError.

    Returns the resolved output_json_file_name.
    """
    if len(file_dict) == 2:
        if set(file_dict.values()) == {'N', 'Y'}:
            n_file = [key for key, value in file_dict.items() if value == 'N'][0]
            y_file = [key for key, value in file_dict.items() if value == 'Y'][0]

            # Read the contents of the 'N' file as JSON
            n_file_json = rj.read_json_file(n_file)
            # Read the contents of the 'Y' file as JSON
            y_file_json = rj.read_json_file(y_file)

            filtered_n_file = [obj for obj in n_file_json if obj['IDENTIFIER'] not in retried_securities]
            for record in y_file_json:
                filtered_n_file.append(record)

            # Write the JSON data to the file with indentation
            with open(output_json_file_name, "w") as f:
                f.write(json.dumps(filtered_n_file, indent=4))
    else:
        if len(file_dict) == 1:
            file_name = list(file_dict.keys())[0]
            output_json_file_name = file_name
        else:
            logger.error(f"{logger_prefix} There should be either 1 or 2 entries in bbg_api_response_details. Output file not produced for security category {security_category}.")
            raise ValueError(f"There should be either 1 or 2 entries in bbg_api_response_details. Output file not produced for security category {security_category}.")

    return output_json_file_name


def _resolve_output_json_file(security_category: str, file_dict: dict, output_json_file_name: str, retried_securities: list) -> str:
    """
    Dispatch to the correct JSON-resolution logic based on security_category.
    Returns the resolved output_json_file_name.
    """
    if security_category == 'securityMaster':
        return _resolve_security_master_json(file_dict, output_json_file_name, retried_securities)

    elif security_category == 'pricingData':
        file_name = list(file_dict.keys())[0]
        return file_name

    return output_json_file_name


def _write_and_update_output(output_json_file_name: str, output_csv_file_name: str,
                              bus_date: str, task_inst: int, run_mode: str, security_category: str) -> None:
    """
    Convert the resolved JSON file to CSV, add the business date column,
    and update bbg_api_response_details with the final shared output file path.
    """
    if os.path.exists(output_json_file_name):
        logger.info(f"{logger_prefix} Converting JSON file : {output_json_file_name} to CSV file : {output_csv_file_name}")
        jw.json_to_csv(output_json_file_name, output_csv_file_name, output_param_delimiter)
        logger.info(f"{logger_prefix} Converted JSON to CSV file {output_csv_file_name}")

    logger.info(f"{logger_prefix} Adding business date column on csv file : {output_csv_file_name}")
    add_busdate_column(output_csv_file_name, output_param_delimiter)
    logger.info(f"{logger_prefix} Added business date column on csv file : {output_csv_file_name}")

    update_final_file_sql = f"UPDATE bbg_api_response_details SET output_shared_file = '{output_csv_file_name}' WHERE business_date = TO_DATE('{bus_date}', 'YYYYMMDD') AND task_instance = {task_inst} AND run_mode = '{run_mode}' AND security_category = '{security_category}' AND output_status = 'completed'"
    logger.info(f"{logger_prefix} Executing sql : {update_final_file_sql}")
    exec_sql(update_final_file_sql)


def produce_output_file(bus_date: str, task_inst: int, run_mode: str, security_category: str, retried_securities: list) -> None:
    logger.info(f"{logger_prefix} Producing output file called for security category : {security_category} and task_instance : {task_inst} for business_date : {bus_date}")

    try:
        retrieve_file_sql = f"select output_file_path, is_retry FROM BBG_API_RESPONSE_DETAILS WHERE task_instance = {task_inst} AND business_date = TO_DATE('{bus_date}', 'YYYYMMDD') AND run_mode = '{run_mode}' AND security_category = '{security_category}' AND output_shared_file is null"
        logger.info(f"{logger_prefix} Executing sql : {retrieve_file_sql}")
        output_file_data = exec_sql(retrieve_file_sql)

        if output_file_data:
            file_dict = dict(output_file_data)

            output_file_name = _build_output_file_name(security_category)

            if output_file_name:
                json_file_extension = str(str(output_file_name).split('.')[0])+'.json'
                output_json_file_name = os.path.join(download_path, json_file_extension)
                output_csv_file_name = os.path.join(download_path, output_file_name) if output_file_name else None

                logger.info(f"{logger_prefix} Creating final json file for security_category : {security_category}.")
                output_json_file_name = _resolve_output_json_file(security_category, file_dict, output_json_file_name, retried_securities)

                logger.info(f"{logger_prefix} output_json_file_name : {output_json_file_name}")
                logger.info(f"{logger_prefix} output_csv_file_name : {output_csv_file_name}")

                _write_and_update_output(output_json_file_name, output_csv_file_name, bus_date, task_inst, run_mode, security_category)
            else:
                logger.error(f"{logger_prefix} Output file name not exists on ts_task_output_params for task : {task_name}.")
                raise FileNotFoundError(f"Output file name not exists on ts_task_output_params for task : {task_name} during produce_output_file.")
        else:
            logger.warning(f"{logger_prefix} File already produced or output record not found for security category : {security_category} and task_instance : {task_inst} for business_date : {bus_date}.")
    except Exception as e:
        raise RuntimeError(f"Unexpected exception occurred on produce_output_file for security category : {security_category}, task_instance : {task_inst} and business_date : {bus_date} as {e}")


class DLRestApiSession(OAuth2Session):
    """Custom session class for making requests to a DL REST API using OAuth2 authentication."""

    oauth2_endpoint = "https://bsso.blpprofessional.com/ext/api/as/token.oauth2"
    instances = {}

    def __new__(cls, client_id: str, client_secret: str, *args, **kwargs):
        """
        Provide a global point of access to the instance of this class.
        Ensure only one instance per client_id is created.
        """

        if not cls.instances.get(client_id):
            cls.instances[client_id] = super().__new__(cls)

        return cls.instances[client_id]

    def __init__(self, client_id: str, client_secret: str, *args, **kwargs):
        """
        Initialize a DLRestApiSession instance.
        """
        '''Creates an instance of BackendApplicationClient using the client_id'''
        self.client = BackendApplicationClient(client_id=client_id)

        """
        Initialize the parent class (OAuth2Session) with the client object.
        The parent class does not need the client_secret at this point because the client_secret is used when fetching the token, 
        not during the initialization of the session
        """
        super().__init__(client=self.client, *args, **kwargs)

        self.client_id = client_id
        self.client_secret = client_secret

        '''Initialize the variable to store the token expiration time'''
        self._token_expires_at = 0

        '''Method fetches the OAuth2 token using the client_secret'''
        self._request_token()

    def _request_token(self):
        """
        Fetch an OAuth2 access token by making a request to the token endpoint.
        """
        self.access_token = self.fetch_token(
            token_url=self.oauth2_endpoint,
            client_secret=self.client_secret
        ).get("access_token")

        decoded_data = jwt.decode(
            jwt=self.access_token, options={"verify_signature": False}
        )
        self._token_expires_at = decoded_data["exp"]

    def _ensure_valid_token(self):
        """
        Ensure that OAuth2 access token is valid. Fetch a new token if the token is expired.
        """
        if not self.access_token:
            raise RuntimeError("\n\tAccess token error")

        valid_min_duration_threshold = 5
        seconds_until_expiration = self._token_expires_at - time.time()

        if valid_min_duration_threshold >= seconds_until_expiration:
            self._request_token()


    def request(self, *args, **kwargs):
        """
        Override the parent class method to request OAuth2 token.
        :return: response object from the API request
        """
        if kwargs.get("url") != self.oauth2_endpoint:
            self._ensure_valid_token()

        return super().request(*args, **kwargs)

    def send(self, request, **kwargs):
        """
        Override the parent class method to log request and response information.
        :param request: prepared request object
        :return: response object from the API request
        """
        logger.info(
            f"{logger_prefix} Request being sent to HTTP server: %s, %s, %s",
            request.method,
            request.url,
            request.headers,
        )

        response = super().send(request, **kwargs)

        logger.info(f"{logger_prefix} Response status: {response.status_code}")
        logger.info(f"{logger_prefix} Response x-request-id: {response.headers.get('x-request-id')}")

        try:
            response.raise_for_status()
        except HTTPError as exc:
            logger.error(f"{logger_prefix} \tUnexpected response status code: {str(response.status_code)}\nDetails: {response.json()}")
            raise exc
        else:
            # Filter out SSE and file download responses.
            if response.headers.get(
                    "Content-Type"
            ) != "text/event-stream" and not response.headers.get(
                "Content-Disposition"
            ) and response.content:
                logger.info(f"{logger_prefix} Response content received... for x-request-id : {response.headers.get('x-request-id')}")
            else:
                logger.info(f"{logger_prefix} Response content either not received or not displayed inline or it is not an Server Send Event (SSE).")

        return response

# main #
app_name = os.getenv('APP_NAME')
task_name = os.getenv('AXIOM_SEC_TASK_NAME')
extract_taskname = os.getenv('EXTRACT_TASK_NAME')
dag_ref = os.getenv('DAG_REF')
node_name = os.getenv('NODE_NAME')

## read/define credentials
client_id = os.getenv("BBG_CLIENT_ID")
client_secret = os.getenv("BBG_CLIENT_SECRET")
cred_expiry_date = os.getenv("BBG_EXPIRATION_DATE")
TIMEZONE = timezone.utc


def _poll_for_output(session, responses_url: str, params: dict, expiration_timestamp, catalog_id: str, reply_timeout_minutes: int) -> tuple:
    """
    Poll the content responses endpoint until output is ready or timeout expires.
    Returns (output_key, output_url, output_file_path).
    """
    # Poll the content responses endpoint until the output is ready or the timeout expires.
    while datetime.now(TIMEZONE) < expiration_timestamp:
        try:
            content_responses = session.get(responses_url, params=params)
            response_contains = json.loads(content_responses.text)['contains']

            # Check if the response contains any output.
            if len(response_contains) > 0:
                output = response_contains[0]
                output_key = output['key']
                output_url = urljoin(
                    HOST,
                    '/eap/catalogs/{c}/content/responses/{key}'.format(c=catalog_id, key=output_key)
                )
                output_file_path = os.path.join(download_path, output_key)
                return output_key, output_url, output_file_path
            else:
                # We make a delay between polls to reduce network usage.
                # If the output is not ready, log a message and wait for a specified interval before polling again.
                logger.info(f"{logger_prefix} Content not ready for download yet. Waiting...")
                time.sleep(poll_timer_seconds)
        except Exception as e:
            logger.warning(f"{logger_prefix} May be a network delay during polling {str(e)}. Retrying in 30 seconds...")
            time.sleep(poll_timer_seconds)
            continue
    logger.error(f"{logger_prefix} Response not received within {reply_timeout_minutes} minutes. Exiting.")
    return None, None, None


def _download_output_file(session, output_url: str, output_file_path: str, output_key: str) -> dict:
    """
    Download the output file from the given URL, write it to output_file_path,
    and decode the content as JSON.
    Handles gzip-encoded responses. Returns the parsed output_json dict.
    """
    output_json = {}

    # Download the file.
    with session.get(output_url, stream=True) as response:
        response.raise_for_status()  # Fail fast on HTTP errors

        # Set the output filename and file path.
        output_filename = output_key
        logger.info(f"{logger_prefix} response headers : {response.headers}")
        logger.info(f"{logger_prefix} output_filename : {output_filename}")
        logger.info(f"{logger_prefix} output_file_path : {output_file_path}")

        # Handle gzip-encoded responses if present.
        if 'content-encoding' in response.headers:
            logger.info(f"{logger_prefix} response.headers['content-encoding'] : {response.headers['content-encoding']}")
            if response.headers['content-encoding'] == 'gzip':
                response.raw.decode_content = True

        logger.info(f"{logger_prefix} response raw : {response.raw}")

        # Read the raw response into a BytesIO buffer.
        raw_buffer = io.BytesIO()
        #############################################
        # Copying the whole raw socket object at once
        shutil.copyfileobj(response.raw, raw_buffer)

        # Write the buffer content to a file.
        raw_buffer.seek(0)
        with open(output_file_path, 'wb') as output_file:
            logger.info(f"{logger_prefix} Loading file from: {output_url} (can take a while) ...")
            shutil.copyfileobj(raw_buffer, output_file)

        ##################################################
        # Decode the in-memory bytes as JSON
        # Reset again to the start of the buffer
        # Decode the buffer content as JSON.
        raw_buffer.seek(0)
        raw_bytes = raw_buffer.read()

        try:
            output_json = json.loads(raw_bytes.decode("utf-8"))
            logger.info(f"{logger_prefix} JSON payload received on path : {output_file_path}")
        except json.JSONDecodeError as e:
            # If the payload is not JSON, you can keep the raw bytes or handle differently
            logger.error(f"{logger_prefix} Payload is not a valid JSON : {e}")
            # Keep the raw bytes in a variable for later processing
            raw_data = raw_bytes
            logger.error(f"{logger_prefix} Payload is not a valid JSON raw_data : {raw_data}")

    return output_json


def _handle_retry_logic(output_json: list, is_retry: str, security_category: str, retried_securities: list) -> None:
    """
    Update bbg_extract_securities with DL request details and RC codes.
    For is_retry == 'N' and securityMaster: detects RC=10/CUSIP records and
    triggers a recursive retry call via request_bbg_api.
    For is_retry == 'Y': updates the retry records with ISIN identifier type.
    """
    if is_retry == 'N':
        update_sql = f"UPDATE bbg_extract_securities SET dl_request_id = :1, dl_request_name = :2, rc_code = :3, dl_snapshot_start_time = TO_TIMESTAMP(:4, 'YYYY-MM-DD\"T\"HH24:MI:SS'), is_retry = '{is_retry}' WHERE task_instance = {extract_task_instance} AND business_date = TO_DATE('{business_date[1]}', 'YYYYMMDD') AND security_category = '{security_category}' AND security = :5"
        # update RC and other details along with securities on bbg_extract_securities
        params = [(record['DL_REQUEST_ID'], record['DL_REQUEST_NAME'], record['RC'], record['DL_SNAPSHOT_START_TIME'], record['IDENTIFIER']) for record in output_json]
        execmany_sql(update_sql, params)

        # condition to make retry request only for security master
        if security_category == 'securityMaster':
            logger.info(f"{logger_prefix} Analyzing the RC=10 records for retry...")
            sql = f"SELECT security, identifier_type FROM bbg_extract_securities WHERE task_instance = {extract_task_instance} AND business_date = TO_DATE('{business_date[1]}', 'YYYYMMDD') AND security_category = '{security_category}' AND rc_code = 10 AND IDENTIFIER_TYPE = 'CUSIP' AND is_retry = 'N'"
            records = exec_sql(sql)

            logger.info(f"{logger_prefix} Total records found for RC=10 and Identifier_type as CUSIP : {len(records)} ")

            if records:
                logger.info(f"{logger_prefix} Date : {datetime.now()}, Securities with RC code with 10 for identifier_type as CUSIP exists. re-try in process.")
                sec_data = [('SECURITY', 'IDENTIFIER_TYPE')]
                for item in records:
                    # replace CUSIP to ISIN directly into the retry securities list
                    sec_data.append((item[0], 'ISIN'))
                    retried_securities.append(item[0])

                logger.info(f"{logger_prefix} Analyzing sec_data before retry : {sec_data}")
                if len(sec_data) > 1:
                    retry_sec_str = "'"+"','".join(str(sec).strip() for sec in retried_securities)+"'"
                    # updating the bbg_extract_securities is_retry to Y to avoid multiple runs
                    update_retry_sql = f"UPDATE bbg_extract_securities SET is_retry = 'Y' WHERE task_instance = {extract_task_instance} AND business_date = TO_DATE('{business_date[1]}', 'YYYYMMDD') AND security_category = '{security_category}' AND security in ({retry_sec_str}) AND identifier_type = 'CUSIP'"

                    logger.info(f"{logger_prefix} update_retry_sql : {update_retry_sql}")
                    exec_sql(update_retry_sql)
                    logger.info(f"{logger_prefix} Update to bbg_extract_securities on is_retry is set to Y for security category : {security_category}.")

                    logger.info(f"{logger_prefix} Requesting a retry call for security category : {security_category} and updating retry to Y")
                    # make a retry call with security data for securityMaster category alone
                    request_bbg_api(security_category, sec_data, 'Y')

    else:
        update_sql = f"UPDATE bbg_extract_securities SET dl_request_id = :1, dl_request_name = :2, rc_code = :3, dl_snapshot_start_time = TO_TIMESTAMP(:4, 'YYYY-MM-DD\"T\"HH24:MI:SS'), identifier_type = 'ISIN' WHERE task_instance = {extract_task_instance} AND business_date = TO_DATE('{business_date[1]}', 'YYYYMMDD') AND security_category = '{security_category}' AND security = :6 AND rc_code = 10 AND identifier_type = 'CUSIP'"
        # update RC and other details along with securities on bbg_extract_securities
        params = [(record['DL_REQUEST_ID'], record['DL_REQUEST_NAME'], record['RC'],
                   record['DL_SNAPSHOT_START_TIME'], record['IDENTIFIER']) for record in output_json]
        execmany_sql(update_sql, params)


def request_bbg_api(security_category: str, security_data = None, is_retry: str = 'N') -> None:
    try:

        logger.info(f"{logger_prefix} Started for security category : {security_category} with retry as : {is_retry}")

        if is_retry == 'Y':
            logger.info(f"{logger_prefix} Retry call initiated for security category : {security_category}")

        dbm.update_bbg_api_details(ts_instance, run_mode, business_date[1], security_category, is_retry)
        logger.info(f"{logger_prefix} Inserted into bbg_api_response_details table for security_category : {security_category}")
        request_short_names={'securityMaster':'SM', 'pricingData':'PL'}
        process_date = datetime.now().strftime('%Y%m%d%H%M%S')
        logger.info(f"{logger_prefix} Process Date : {process_date}")
        SESSION = DLRestApiSession(client_id=client_id, client_secret=client_secret)

        # This is a required header for each call to DL Rest API.
        SESSION.headers['api-version'] = '2'

        # request for creating payload json
        payload_string = trigger_payload(security_category, dag_ref, business_date[1], run_mode, security_data)

        if not payload_string:
            logger.error(f"{logger_prefix} Request Payload string not received from createPayloadJson.py. Exiting.")
            raise ValueError(f"Request Payload string not received from createPayloadJson.py for security_category : {security_category} , dag_ref : {dag_ref} and business_date : {business_date[1]}. Exiting.")
        logger.info(f"{logger_prefix} Payload request string got for {security_category} category.")

        payload_json = rj.str_to_json(payload_string)
        if not payload_json:
            logger.error(f"{logger_prefix} Can't able to convert payload string to json for request : {req}. Exiting.")
            raise ValueError(f"Can't able to convert payload string to json for security_category : {security_category}, dag_ref : {dag_ref} and business_date : {business_date[1]}. Exiting.")
        logger.info(f"{logger_prefix} Payload request string convert into JSON for {security_category} category.")

        request_name = f"unisrc{security_category}{process_date}"  # processdate contains date and time to identify request from data GO
        identifier = f"unisrc{str(request_short_names.get(security_category))}{str(uuid.uuid1())[:6]}" # uuid reduces likelihood of ID overlaps
        payload_json['name'] = request_name
        payload_json['identifier'] = identifier

        ############################################################################
        '''Construct the URL for the catalogs endpoint by joining the base host URL with the catalogs endpoint path.'''
        catalogs_url = urljoin(HOST, '/eap/catalogs/')

        '''Make a GET request to the catalogs endpoint to retrieve the list of available catalogs.'''
        response = SESSION.get(catalogs_url)
        logger.info(f"{logger_prefix} {response}")

        '''Parse the JSON response to extract the list of catalogs.'''
        catalogs = response.json()['contains']

        '''Iterate over each catalog in the list.'''
        for catalog in catalogs:

            '''Check if the catalog has a subscription type of 'scheduled'.'''
            if catalog['subscriptionType'] == 'scheduled':
                # Take the catalog having "scheduled" subscription type,
                # which corresponds to the Data License account number.
                catalog_id = catalog['identifier']
                logger.info(f"{logger_prefix} catalog_id : {catalog_id} and catalog['subscriptionType'] : {catalog['subscriptionType']}")

                '''Break the loop as we have found the required catalog.'''
                break
        else:
            '''If the loop completes without finding a 'scheduled' catalog, log an error.'''
            logger.error(f"{logger_prefix} Scheduled catalog not in %r", response.json()['contains'])

            '''Raise a RuntimeError to indicate that the required catalog was not found.'''
            raise RuntimeError('Scheduled catalog not found')

        ############################################################################
        # # Request
        ############################################################################
        # - Create the request component.

        '''Assign the payload JSON to the variable request_payload.'''
        request_payload = payload_json

        '''Log the request component payload for debugging purposes.'''
        logger.info(f"{logger_prefix} Request component payload for name : {request_payload['name']} and identifier : {request_payload['identifier']}")

        # Create a new request resource.
        '''Construct the URL for the catalog endpoint by joining the base host URL with the catalog path.'''
        catalog_url = urljoin(HOST, '/eap/catalogs/{c}/'.format(c=catalog_id))

        '''Construct the URL for the requests endpoint by appending 'requests/' to the catalog URL.'''
        requests_url = urljoin(catalog_url, 'requests/')
        logger.info(f"{logger_prefix} requests_url : {requests_url}")

        '''Make a POST request to the requests endpoint with the request payload.'''
        response = SESSION.post(requests_url, json=request_payload)
        logger.info(f"{logger_prefix} response : {response}")
        logger.info(f"{logger_prefix} response status_code : {response.status_code}")

        # Extract the URL of the created resource.
        '''Extract the URL of the created resource from the response headers.'''
        request_location = response.headers['Location']

        '''Construct the full URL of the created resource by joining the base host URL with the location path.'''
        request_url = urljoin(HOST, request_location)

        logger.info(f"{logger_prefix} request_location : {request_location}")
        logger.info(f"{logger_prefix} request_url : {request_url}")

        # Extract the identifier of the created resource.
        '''Parse the response text to extract the identifier of the created resource.'''
        request_id = json.loads(response.text)['request']['identifier']
        logger.info(f"{logger_prefix} request_id : {request_id}")

        logger.info(f"{logger_prefix} {request_name} resource has been successfully created at {request_url}")

        # ############################################################################
        # - Inspect the newly-created request component.
        '''Make a GET request to the newly created request URL to inspect the resource.'''
        SESSION.get(request_url)

        # ############################################################################
        ## Poll the Content Endpoint with Prefix and Show Listing

        ############################################################################
        # - Poll '/content/responses/' endpoint to find out if the output is ready
        # for download.
        '''Construct the URL for the content responses endpoint by joining the base host URL with the content responses path.'''
        responses_url = urljoin(HOST, '/eap/catalogs/{c}/content/responses/'.format(c=catalog_id))

        # Filter the required output from the available content by passing the request_name as prefix
        # and request_id as unique requestIdentifier query parameters respectively.
        '''Define query parameters to filter the required output by request_name as prefix and request_id as unique requestIdentifier.'''
        params = {
            'prefix': request_name,
            'requestIdentifier': request_id,
        }

        # We recommend adjusting the polling frequency and timeout based on the amount of data or the time range requested.

        '''Set a timeout for the polling process (e.g., 45 minutes).'''
        reply_timeout_minutes = 45
        reply_timeout = timedelta(minutes=reply_timeout_minutes)
        expiration_timestamp = datetime.now(TIMEZONE) + reply_timeout

        logger.info(f"{logger_prefix} reply_timeout_minutes : {reply_timeout_minutes}")
        logger.info(f"{logger_prefix} reply_timeout : {reply_timeout}")
        logger.info(f"{logger_prefix} expiration_timestamp : {expiration_timestamp}")

        '''Poll the content responses endpoint until the output is ready or the timeout expires.'''
        output_key, output_url, output_file_path = _poll_for_output(SESSION, responses_url, params, expiration_timestamp, catalog_id, reply_timeout_minutes)

        ############################################################################
        # - Download the file.
        output_json = _download_output_file(SESSION, output_url, output_file_path, output_key)

        '''logger the successful download of the file.'''
        logger.info(f"{logger_prefix} File downloaded: {output_key}")
        logger.info(f"{logger_prefix} File location: {output_file_path}")

        '''output json analysis'''
        total_records = len(output_json)
        valid_records = len([record for record in output_json if record['RC'] == 0])
        dl_request_id = output_json[0]['DL_REQUEST_ID']
        dl_request_name = output_json[0]['DL_REQUEST_NAME']

        '''update the completion status with output file path'''
        dbm.update_bbg_api_details(ts_instance, run_mode, business_date[1], security_category, is_retry, 'completed', dl_request_id, dl_request_name, total_records, valid_records, output_file_path)

        _handle_retry_logic(output_json, is_retry, security_category, retried_securities)

        produce_output_file(business_date[1], ts_instance, run_mode, security_category, retried_securities)
        logger.info(f"{logger_prefix} Output file produced and shared to NAS Path")

    except Exception as e:
        logger.error(f"{logger_prefix} An exception occurred : {e}")
        dbm.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'error', 'error')
        dbm.update_bbg_api_details(ts_instance, run_mode, business_date[1], security_category, is_retry, 'error')
        dbm.update_ts_task_status(app_name, extract_taskname, business_date[1], dag_ref, run_mode, 'error', EXTRACT_COMPLETE_MSG)
        sys.exit(1)


if __name__ == "__main__":
    business_date = dbm.get_app_business_date(app_name)

    run_mode = str(dag_ref).split('__')[0].split('-')[1]
    try:
        dbm.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'processing', 'processing bbg api')
        ts_instance = dbm.get_task_instance(app_name, task_name, dag_ref)

        extract_task_instance = dbm.get_task_instance(app_name, extract_taskname, dag_ref)

        global logger_prefix
        logger_prefix = f"[{ts_instance}] [{node_name}]"

        cred_expiry_valid_day = dbm.get_app_config_param(app_name, task_name, 'Bloomberg_credential_expiry_days')
        if not cred_expiry_valid_day:
            logger.error(f"{logger_prefix} Not able to find days to check the Bloomberg credential expiry validation.")
            raise LookupError(f"{logger_prefix} Not able to find days to check the Bloomberg credential expiry validation.")

        cred_expiry = check_credential_expiration(int(cred_expiry_date), int(cred_expiry_valid_day['credExpiryDays']))
        if not cred_expiry:
            logger.error(f"{logger_prefix} Credentials expired...exiting. Expiry date : {cred_expiry_date}")
            raise PermissionError(f"Credentials expired...exiting. Expiry date : {cred_expiry_date}")
        else:
            logger.info(f"{logger_prefix} Credential expiry valid...proceeding.")

        payloads = dbm.get_app_config_param(app_name, task_name, 'Bloomberg_payload_names')
        if not payloads:
            logger.error(f"{logger_prefix} Payload names or security category data not exists in config for : Bloomberg_payload_names")
            raise LookupError("Payload names or security category data not exists in config for : Bloomberg_payload_names")
        else:
            logger.info(f"{logger_prefix} payloads for task_name : {task_name} , {payloads}")

        logger.info(f"{logger_prefix} payloads : {payloads}")
        payload_request_names=[item['name'] for item in payloads['payloads']]

        output_params = dbm.get_output_params(app_name, task_name, 'Bloomberg_api_output_filepath')
        logger.info(f"{logger_prefix} output_params : {output_params}")

        output_mode = output_params[0][0] if output_params[0][0] else None
        download_path = output_params[0][1] if output_params[0][1] else None
        output_param_filename = output_params[0][2] if output_params[0][2] else None
        output_param_datefmt = output_params[0][3] if output_params[0][3] else None
        output_param_delimiter = output_params[0][4] if output_params[0][4] else ','

        if not download_path:
            logger.error(f"{logger_prefix} Output path not exists for config : Bloomberg_api_output_filepath on ts_task_output_params table. exiting..")
            raise FileNotFoundError("Output path not exists for config : Bloomberg_api_output_filepath on t_task_output_params table. exiting.")
        else:
            logger.info(f"{logger_prefix} Bloomberg API download path : {download_path}")

        if not output_param_filename or not output_param_datefmt:
            logger.error(f"{logger_prefix} Output filename or date format not exists on config : Bloomberg_api_output_filepath on ts_task_output_params table. exiting..")
            raise LookupError("Output filename or date format not exists on config : Bloomberg_api_output_filepath on ts_task_output_params table. exiting..")
        else:
            logger.info(f"{logger_prefix} Bloomberg output file name and date format exists.. {output_param_filename} and {output_param_datefmt}")

        ## API end points
        bbg_api_hosts = dbm.get_app_config_param(app_name, task_name, 'Bloomberg_api_host')
        if not bbg_api_hosts:
            logger.error(f"{logger_prefix} Api Host and authentication end point URL not exists on config : Bloomberg_api_host. exiting.")
            raise LookupError("Api Host and authentication end point URL not exists on config : Bloomberg_api_host. exiting.")
        else:
            logger.info(f"{logger_prefix} bbg_api_hosts : {bbg_api_hosts}")

        logger.info(f"{logger_prefix} Setting Host and OAuth2_endpoint for API call...")
        # API end points
        HOST = bbg_api_hosts['apiHost']
        oauth2_endpoint_url = bbg_api_hosts['oauth2_endpoint']

        api_polltimer=dbm.get_app_config_param(app_name, task_name, 'Bloomberg_api_poll_timer')
        poll_timer_seconds=int(api_polltimer.get('apiPollTimer', 30))
        logger.info(f"{logger_prefix} Api polling time : {poll_timer_seconds} seconds.")

        # setting empty list for retried securities and this will happen only once for security Master category
        retried_securities = []

        for req in payload_request_names:
            logger.info(f"{logger_prefix} Requesting for payload : {req}")
            request_bbg_api(req)

        dbm.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'completed', 'bbg api completed')
        dbm.update_ts_task_status(app_name, extract_taskname, business_date[1], dag_ref, run_mode, 'completed', EXTRACT_COMPLETE_MSG)

        logger.info(f"{logger_prefix} Task {task_name} status updated to completed successfully.")

    except Exception as e:
        logger.error(f"{logger_prefix} An exception occurred : {e}")
        dbm.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'error', 'error')
        dbm.update_ts_task_status(app_name, extract_taskname, business_date[1], dag_ref, run_mode, 'error', EXTRACT_COMPLETE_MSG)
        sys.exit(1)