import os
import sys
import re
import time
import oracledb
from datetime import datetime
from glob import glob
from create_logger import get_logger
import common_sql_functions as csf
import get_business_date as bd
from databaselib import exec_sql

# LOGGING CONFIG
logger = get_logger()

def file_check(directory, file_pattern):
    return glob(os.path.join(directory, '**', file_pattern), recursive=True)

def _check_matched_files(matched: list, directory: str, file_name: str, file_pattern: str, missing_files: list) -> None:
    """
    Evaluate the glob match result for a single file pattern:
    - Exactly 1 match: log it.
    - More than 1 match: log error and raise RuntimeError (duplicate files).
    - 0 matches: append to missing_files and log warning.
    """
    if matched:
        if len(matched) == 1:
            for f in matched:
                logger.info(f"{logger_prefix} Matched file: {f}")
        elif len(matched) > 1:
            logger.error(f"{logger_prefix} Duplicate files found : {matched} in directory : {directory} for file : {file_name}")
            raise RuntimeError(f"Exception occurred due to duplicate files found : {matched} in directory : {directory} for file : {file_name}")
        else:
            missing_files.append(f"{directory}/{file_pattern}")
            logger.warning(f"{logger_prefix} Missing file detected {directory}/{file_name}")
    else:
        missing_files.append(f"{directory}/{file_pattern}")
        logger.warning(f"{logger_prefix} Missing file detected {directory}/{file_name}")


def process_files(app_name, extract_task_name):

    extract_task_id = csf.get_task_id(app_name, extract_task_name)
    missing_files = []
    sql = (f"SELECT file_pattern, file_path, source_path, date_format, threshold_time FROM ts_file_watcher WHERE task_id = {extract_task_id} AND status = 'A'")

    try:
        records = exec_sql(sql)

        # Initialise threshold_time with None it will be set from the first row
        threshold_time = None

        for file_pattern, file_path, source_path, date_format, th_time in records:

            # Capture the threshold value (assuming it is the same for all rows)
            if threshold_time is None:
                threshold_time = th_time 

            directory = os.path.join(file_path, source_path)
            bus_date = csf.get_app_business_date(app_name)

            file_name = str(file_pattern).replace('{currdate}', bd.convert_to_date_format(bus_date[1], date_format)).replace('{prevdate}', bd.convert_to_date_format(bus_date[0], date_format)).replace('{nextdate}', bd.convert_to_date_format(bus_date[2], date_format)).replace('{nextcalendardate}', bd.convert_to_date_format(bus_date[1], date_format, 1))

            matched = file_check(directory, file_name)

            _check_matched_files(matched, directory, file_name, file_pattern, missing_files)

        return missing_files, threshold_time

    except Exception as e:
        logger.error(f"{logger_prefix} Exception occurred on process_files for app_name : {app_name} and task_name : {extract_task_name}: {e}")
        raise RuntimeError(f"Exception occurred during process_files for app_name : {app_name} and extract_task_name : {extract_task_name}")

def _parse_threshold_time(threshold_time):
    """Convert threshold_time (str, datetime, or time) to a datetime.time object."""
    # ----- convert threshold to datetime.time -----
    if isinstance(threshold_time, str):
        try:
            return datetime.strptime(threshold_time, "%H:%M:%S").time()
        except ValueError:
            return datetime.strptime(threshold_time, "%H:%M").time()
    elif isinstance(threshold_time, datetime):
        return threshold_time.time()
    else:
        return threshold_time          # already a time object


def _handle_missing_files(missing_files, threshold_time, logger_prefix, retry_interval):
    """
    Handle the case where files are missing during the watch loop.

    Returns True  -> break out of the while loop (threshold exceeded, partial success).
    Returns False -> sleep and continue retrying.
    Raises RuntimeError -> all files missing after threshold, fail the job.
    """
    threshold_obj = _parse_threshold_time(threshold_time)
    now = datetime.now().time()

    if now > threshold_obj:

        # ----------- Decision based on how many files are missing ----------
        if len(missing_files) < 8:          # at least one file **is** present
            logger.error(
                f"{logger_prefix} Threshold ({threshold_obj}) exceeded, "
                f"but {len(missing_files)}  expected files are missing. "
                f"Missing files: {missing_files}. "
                "Marking job as SUCCESS."
            )
            # Break out of the retry loop downstream code will mark the run as completed
            return True
        else:                               # ALL 8 files are still missing
            logger.error(
                f"{logger_prefix} Threshold ({threshold_obj}) exceeded and **ALL** "
                f"8 expected files are missing. Missing files: {missing_files}. "
                "Failing the job."
            )
            # Raise an exception so the outer try/except treats this as a failure
            raise RuntimeError("All expected files missing after threshold job failed")

    # Normal retry path
    time.sleep(retry_interval)
    return False


def main():
    app_name = os.getenv("APP_NAME")
    task_name = os.getenv("TASK_NAME")
    extract_task_name = os.getenv("EXTRACT_TASK_NAME")
    dag_ref = os.getenv("DAG_REF")
    node_name = os.getenv("NODE_NAME")
    run_mode = str(dag_ref).split('__')[0].split('-')[1]
    retry_interval=900

    try:
        if not task_name or not app_name:
            raise KeyError("task_name or app_name. Environment variables for these variables are not set.")

        # Get application business date
        business_date = csf.get_app_business_date(app_name)

        # Create a insert in ts_task_status table
        csf.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'processing', 'processing file watcher')

        # Get task_instance from ts_task_stats table
        ts_instance = csf.get_task_instance(app_name, task_name, dag_ref)

        # set a global variable for logger_prefix
        global logger_prefix
        logger_prefix = f"[{ts_instance}] [{node_name}]"

        while True:
            missing_files, threshold_time = process_files(app_name, extract_task_name)

            if missing_files:
                should_break = _handle_missing_files(missing_files, threshold_time, logger_prefix, retry_interval)
                if should_break:
                    break
                continue
            else:
                logger.info(f"{logger_prefix} All files are present")
                break

        csf.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'completed', 'file watcher completed')

        logger.info (f"{logger_prefix} File watcher task : {task_name} completed for business date : {business_date[1]} and dag_ref : {dag_ref}")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Exception caught : {e}")
        csf.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'error', 'error processing file watcher')
        sys.exit(1)

if __name__ == "__main__":
    main()