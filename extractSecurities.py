import os
import sys
import re
import time
import pytz
import argparse
import openpyxl
from datetime import datetime
import subprocess
from glob import glob
from pathlib import Path
from pandas.errors import ParserWarning
from create_logger import get_logger
from create_logger import get_logger as lg
from typing import List, Dict, Tuple, Set, Callable, Any, Optional, Union
from read_cro_file import load_fixed_length
import read_json
import pandas as pd
from functools import wraps
import common_sql_functions as dbm
from databaselib import exec_sql, execmany_sql
import get_business_date as bd


logger = get_logger()

def timer_decorator(log: lg) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
        Decorator to log the execution time of a function.

        This decorator wraps a function to log its execution time, including the function name,
        arguments, keyword arguments, and the result. It also logs the time taken for the function
        to execute.

        Parameters:
        log (lg.Logger): A logger instance used to log the function's execution details.

        Returns:
        Callable[[Callable[..., Any]], Callable[..., Any]]: A decorator function that wraps the target function.

        Example:
        @timer_decorator(logger)
        def example_function(x, y):
             return x + y

        result = example_function(3, 4)
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """
            Decorator function that wraps the target function.

            Parameters:
                func (Callable[..., Any]): The function to be wrapped.

            Returns:
                Callable[..., Any]: The wrapped function.
        """
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            """
                Wrapper function that logs the execution details of the target function.

                Parameters:
                    *args (Any): Positional arguments passed to the target function.
                    **kwargs (Any): Keyword arguments passed to the target function.

                Returns:
                    Any: The result of the target function.
            """
            start_time = time.time()
            log.info(f"{logger_prefix} IN - {func.__name__} - Argument passed") # Args: {args}, Kwargs: {kwargs}
            result = func(*args, **kwargs)
            end_time = time.time()
            time_taken = end_time-start_time
            log.info(f"{logger_prefix} OUT - {func.__name__} - Time taken: {time_taken:.2f} seconds") # Result : result is commented for logger clarity
            return result
        return wrapper
    return decorator

@timer_decorator(logger)
def get_files(files_path: str, file_name: str) -> List[str]:
    """
        Retrieve a list of file names from a specified directory.

        This function takes a directory path and returns a list of file names (excluding directories)
        present in that directory. It logs the list of files found and handles any errors that occur
        during the process.

        Parameters:
        files_path (str): The path to the directory from which to retrieve the file names.
        file_name  (str): file name pattern to do glob search.

        Returns:
        List[str]: A list of file names in the specified directory. Returns an empty list if the directory
                   does not exist or if an error occurs.

        Raises:
        FileNotFoundError: If the specified directory path does not exist.
        Exception: If any other error occurs during the process.

        Example:
        files = get_files('/path/to/directory', 'file name pattern')
        print(files)
        ['file1.txt', 'file2.csv', 'file3.json']
    """
    try:
        files = glob(os.path.join(files_path, '**', f"{file_name}"), recursive=True)

        files = [file for file in files if not file.endswith('.ind')]

        if not files:
            logger.warning(f"{logger_prefix} No file found on path : {files_path}")

        logger.info(f"{logger_prefix} Files found : {files}")

        return files
    except FileNotFoundError as fe:
        logger.error(f"{logger_prefix} The specified path does not exists as {fe}.")
        return []
    except Exception as e:
        logger.error(f"{logger_prefix} An error occurred : {e}")
        return []

@timer_decorator(logger)
def export_dataframe(data_fr: pd.DataFrame, output_path: str, name: str, id_type: str) -> None:
    """
        Export a DataFrame to a CSV file.

        This function takes a DataFrame and exports it to a CSV file. The file is saved in a directory
        specified by the configuration. The filename is constructed using the provided name and identifier type.

        Parameters:
        data_fr (pd.DataFrame): The DataFrame to be exported.
        output_path (str): The path for the output file to be placed.
        name (str): The base name for the output file.
        id_type (str): The identifier type to be included in the filename.

        Returns:
        None

        Raises:
        Exception: If there is an error during the export process, an error message is logged.

        Example:
        df = pd.DataFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
        export_dataframe(df, '/output/path/', 'example.csv', 'type1')
    """
    try:
        file_path=os.path.join(output_path, name+'_'+id_type+'.csv')
        data_fr.to_csv(file_path, index=False)
    except Exception as e:
        logger.error(f"{logger_prefix} Error exporting dataframe to csv file: {e}")


def excel_write(file_path: str, file_name: str, *dataframes: pd.DataFrame, **sheet_names: Dict[str, str]) -> None:
    """
        Write multiple DataFrames to different sheets in an Excel file. Use openpyxl/xlsxwriter engine

        Parameters:
        file_path (str): The path to the Excel file.
        *dataframes (pd.DataFrame): Variable length argument list of DataFrames to write.
        **sheet_names (Dict[pd.DataFrame, str]): Keyword arguments where keys are Dataframes and values are sheet names.
    """
    try:
        with pd.ExcelWriter(os.path.join(file_path, file_name), engine='openpyxl') as writer:
            for idx, df in enumerate(dataframes):
                df.to_excel(writer, sheet_name=sheet_names['sheet'+str(idx+1)], index=False)
    except PermissionError:
        logger.error(f"{logger_prefix} Permission denied to write to filepath : {file_path}, file : {file_name}")
        raise PermissionError(f"Exception occurred on excel_write as PermissionError for file : {file_name} on path : {file_path}.")
    except FileNotFoundError:
        logger.error(f"{logger_prefix} The file path {file_path} does not exist.")
        raise FileNotFoundError(f"Exception occurred on excel_write as FileNotFoundError for file : {file_name} on path : {file_path}.")
    except Exception as e:
        logger.error(f"{logger_prefix} writing data to file. Unexpected error occurred : {e}")
        raise RuntimeError(f"Exception occurred on excel_write for file : {file_name} on path : {file_path} as {e}.")

def create_update_identype(data_fr: pd.DataFrame, identifier_type: str) -> pd.DataFrame:
    """
        Create or update the 'identifier_type' column in a DataFrame.

        This function checks if the 'identifier_type' column exists in the provided DataFrame.
        If the column does not exist, it creates the column and fills it with NaN values.
        It then fills any NaN values in the 'identifier_type' column with the specified identifier type.

        Parameters:
            data_fr (pd.DataFrame): The DataFrame in which to create or update the 'identifier_type' column.
            identifier_type (str): The identifier type to fill NaN values in the 'identifier_type' column.

        Returns:
            pd.DataFrame: The updated DataFrame with the 'identifier_type' column.
    """
    if 'identifier_type' not in data_fr.columns:
        data_fr['identifier_type'] = pd.NA
    data_fr['identifier_type'] = data_fr['identifier_type'].fillna(identifier_type)

    return data_fr

@timer_decorator(logger)
def concat_dataframes(src_df: pd.DataFrame = pd.DataFrame(), dest_df: pd.DataFrame = pd.DataFrame()) -> pd.DataFrame:
    """
        Concatenates two pandas DataFrames.

        This function concatenates the source DataFrame (`srcDf`) to the destination DataFrame (`destDf`).
        If `destDf` is empty, it assigns `srcDf` to `destDf`. Otherwise, it concatenates `srcDf` to `destDf`
        and returns the resulting DataFrame.

        Parameters:
        -----------
        srcDf : pd.DataFrame, optional
            The source DataFrame to be concatenated. Default is an empty DataFrame.
        destDf : pd.DataFrame, optional
            The destination DataFrame to which `srcDf` will be concatenated. Default is an empty DataFrame.

        Returns:
        --------
        pd.DataFrame
            The concatenated DataFrame, which is either `srcDf` if `destDf` was empty,
            or the result of concatenating `srcDf` and `destDf`.

        Raises:
        -------
        NameError
            If `destDf` is not defined, it catches the NameError and logs a message,
            then assigns `srcDf` to `destDf`.
    """
    try:
        if isinstance(dest_df, pd.DataFrame):
            if dest_df.empty:
                dest_df = src_df
            else:
                dest_df = pd.concat([dest_df, src_df], ignore_index=True)

            return dest_df
    except NameError:
        logger.exception(f"{logger_prefix} pricing_df not exists to concat on concat_dataframes.")
        raise NameError("pricing_df not exists to concat on concat_dataframes.")
    except Exception as exp:
        raise RuntimeError(f"An unexpected error occurred on concat_dataframes. {exp}")


def apply_operator_condition(df: pd.DataFrame, cond) -> pd.DataFrame:
    """
        Apply a single condition to the DataFrame.

        Parameters:
        df (pd.DataFrame): The DataFrame to filter.
        condition (dict): The condition to apply.

        Returns:
        pd.DataFrame: The filtered DataFrame.
    """
    try:
      if cond['operator'] == '==':
          df = df[df[cond['column']].str.strip() == str(cond['value']).strip()]
      elif cond['operator'] == '!=':
          df = df[df[cond['column']] != cond['value']]
      elif cond['operator'] == 'in':
          df = df[df[cond['column']].isin(cond['value'])]
      elif cond['operator'] == 'not in':
          df = df[~df[cond['column']].isin(cond['value'])]
      elif cond['operator'] == 'like':
          df = df[df[cond['column']].str.contains(cond['value'].replace('%', '.*'), regex=True)]
      elif cond['operator'] == 'not like':
          df = df[~df[cond['column']].str.contains(cond['value'].replace('%', '.*'), regex=True)]
      else:
          raise ValueError(f"Unsupported operator: {cond['operator']}")
    except KeyError as ke:
        logger.error(f"{logger_prefix} KeyError exception : {ke}")
    except ValueError as ve:
        logger.error(f"{logger_prefix} ValueError exception : {ve}")

    return df

def apply_conditions(filterdf: pd.DataFrame, conditions) -> pd.DataFrame:
    """
        Apply multiple conditions to the DataFrame.

        Parameters:
        filterdf (pd.DataFrame): The DataFrame to filter.
        conditions (list or dict): A list of conditions or a single condition to apply.

        Returns:
        pd.DataFrame: The filtered DataFrame.
    """
    logger.info(f"{logger_prefix} apply_conditions IN... check shape before : {filterdf.shape}")

    try:
        if isinstance(conditions, dict):
            return apply_operator_condition(filterdf, conditions)
        elif isinstance(conditions, list):
            for cond in conditions:
                filterdf = apply_operator_condition(filterdf, cond)
            return filterdf
        elif isinstance(conditions, str):
            raise ValueError("Conditions should be a list or a dictionary, not a string.")
        else:
            raise ValueError("Unsupported type for conditions.")
    except ValueError as ve:
        logger.error(f"{logger_prefix} Exception occurred while apply_conditions : {ve}")


def apply_filter(filter_df, condition):
    logger.info(f"{logger_prefix} Applying filter condition...")
    for filt in condition:
        filter_df = apply_operator_condition(filter_df, filt)

    return filter_df

def apply_delete(del_df, condition):
    try:
        logger.info(f"{logger_prefix} Applying delete condition...")
        for filt in condition:
            if isinstance(filt['value'], list):
                modified_list = [s.replace('%', '.*') for s in filt['value']]
                del_df.drop(del_df[del_df[filt['column']].isin(modified_list)].index, inplace=True)
            elif isinstance(filt['value'], str):
                del_df.drop(del_df[del_df[filt['column']].str.contains(str(filt['value']).replace('%', '.*'), regex=True)].index, inplace=True)
            else:
                raise ValueError("Unsupported type on condition value.")

        return del_df
    except ValueError as ve:
        logger.error(f"{logger_prefix} Exception occurred on apply_delete : {ve}")

def apply_update(update_df, conditions):
    try:
        logger.info(f"{logger_prefix} Applying update condition...")
        for cond in conditions:
            mask = True
            for whr_cond in cond['whereCond']:
                if whr_cond['operator'] == '==':
                    mask = mask & (update_df[whr_cond['column']] == whr_cond['value'])
                elif whr_cond['operator'] == '!=':
                    mask = mask & (update_df[whr_cond['column']] != whr_cond['value'])
                elif whr_cond['operator'] == 'in':
                    mask = mask & (update_df[whr_cond['column']].isin(whr_cond['value']))
                elif whr_cond['operator'] == 'not in':
                    mask = mask & (~update_df[whr_cond['column']].isin(whr_cond['value']))
                else:
                    raise ValueError(f"Unsupported operator : {whr_cond['operator']}. check apply_update function.")

            update_df.loc[mask, cond['column']] = cond['value']

        return update_df
    except ValueError as ve:
        logger.error(f"{logger_prefix} Exception occurred on apply_update : {ve}")

def update_extracted_securities(sec_category: str, task_instance: int, business_date: str, security_data: pd.DataFrame) -> None:
    try:
        delete_sql=f"DELETE FROM bbg_extract_securities WHERE security_category = '{sec_category}' AND task_instance = {task_instance}"
        exec_sql(delete_sql)

        insert_sql=f"INSERT INTO bbg_extract_securities (run_date, business_date, task_instance, security_category, security, identifier_type) VALUES (SYSDATE, TO_DATE('{business_date}', 'YYYYMMDD'), {task_instance}, '{sec_category}', :1, :2)"
        params=[(row['secCol'], row['identifier_type']) for index, row in security_data.iterrows()]
        execmany_sql(insert_sql, params)

        logger.info(f"{logger_prefix} Securities inserted into bbg_extract_securities table for category : {sec_category}, business_date : {business_date}, task_instance : {task_instance}")
    except Exception as exp:
        logger.error(f"{logger_prefix} Exception occurred executing update_extracted_securities : {exp}")
        raise RuntimeError(f"Exception occurred on update_extracted_securities during security category : {sec_category} on task_instance : {task_instance} and business_date : {business_date} as {exp}.")



## main ##
# For default dag ref with timezone format
dt = datetime.now(pytz.utc).isoformat()

app_name = os.getenv('APP_NAME')
task_name = os.getenv('EXTRACT_TASK_NAME')
node_name = os.getenv('NODE_NAME')
dag_ref = os.getenv('DAG_REF')


def _read_delimited_file(file_input_path: str, sep_value: str, cols: list, skip_rows: int) -> pd.DataFrame:
    """
    Read a delimited (CSV) position file into a DataFrame.
    Raises ValueError on parse errors or column mismatches.
    """
    logger.debug(f"{logger_prefix} Config file: {file_input_path}, file_config : {file_config}")

    try:
        df = pd.read_csv(file_input_path, sep=sep_value, skiprows=skip_rows, header=None)
        df.columns = cols

        logger.info(f"{logger_prefix} Read csv into dataframe completed for file : {file_input_path}")
    except ValueError as e:
        logger.exception(f"{logger_prefix} Error while readign or parsing file : {e}")
        logger.error(f"{logger_prefix} Please check the column names in file and ensure they match exactly with app_config_params fileCols : {file_input_path}.")
        raise ValueError(f"Error reading file : {file_input_path} with seperator : {sep_value}.")
    except ParserWarning as e:
        logger.exception(f"{logger_prefix} Warning : {e}")
        raise ValueError(f"Error parsing file through pandas read_csv for file : {file_input_path}. ParseWarning as {e}.")

    return df


def _read_fixed_length_file(file_input_path: str, sep_value: list, skip_rows: int) -> pd.DataFrame:
    """
    Read a fixed-length position file into a DataFrame.
    Uses load_fixed_length to obtain column slice definitions.
    """
    logger.info(f"{logger_prefix} Requesting load for fixed length file : {file_input_path}")
    conf_slice = load_fixed_length(sep_value)

    """
    Implement Fixed length formatting of data
    """
    data = []
    with open(file_input_path, 'r') as file:
        row_index = 0
        # for line in itertools.islice(file, skip_rows, None):
        for line in file:
            if row_index < skip_rows:
                row_index += 1
                continue
            line = line.rstrip()
            row = {}
            for s, field_name in conf_slice:
                row[field_name] = line[s]
            data.append(row)
            row_index += 1

    return pd.DataFrame(data)


def _read_file_into_dataframe(file_input_path: str, file_config: dict, cols: list, skip_rows: int) -> pd.DataFrame:
    """
    Read a position file into a DataFrame based on the separator config.
    Supports 'delimited' (csv) and 'fixedlength' formats.
    Returns the raw DataFrame with columns assigned.
    """
    sep: str | None = file_config['fileParams'].get('separator', {}).get('type', None)
    sep_value: list[dict[str, str | int]] | str | None = file_config['fileParams'].get('separator', {}).get('values', None)

    if sep is not None and sep == 'delimited' and isinstance(sep_value, str):
        return _read_delimited_file(file_input_path, sep_value, cols, skip_rows)

    elif sep is not None and sep == 'fixedlength' and isinstance(sep_value, List):
        return _read_fixed_length_file(file_input_path, sep_value, skip_rows)

    return pd.DataFrame()


def _apply_operations(df: pd.DataFrame, operations) -> pd.DataFrame:
    """
    Apply FILTER / DELETE / UPDATE operations from the file config to the DataFrame.
    Returns the modified DataFrame.
    """
    logger.info(f"{logger_prefix} Filter data on position files based on config condition")
    logger.info(f"{logger_prefix} {operations}")

    if operations is not None:
        for cond in operations:
            if cond['type'] is not None:
                if cond['type'] == 'FILTER':
                    df = apply_filter(df, cond['conditions'])
                elif cond['type'] == 'DELETE':
                    df = apply_delete(df, cond['conditions'])
                elif cond['type'] == 'UPDATE':
                    df = apply_update(df, cond['conditions'])

    return df


def _process_single_file(file_input_path: str, file_config: dict, ind_output: str,
                          output_path: str, security_df: pd.DataFrame,
                          pricing_df: pd.DataFrame) -> tuple:
    """
    Process one matched position file:
      - validates fileParams and iden_type
      - reads into DataFrame (_read_file_into_dataframe)
      - applies FILTER/DELETE/UPDATE operations (_apply_operations)
      - deduplicates
      - exports individual output if ind_output == 'yes'
      - appends to security_df and pricing_df
    Returns updated (security_df, pricing_df).
    """
    try:
        if 'fileParams' in file_config:
            cols: List[str] = file_config['fileParams'].get('fileCols', [])
            skip_rows: int = file_config['fileParams'].get('ignoreLines', 0)
            iden_type: str | None = file_config.get('idenType', None)

            if iden_type is None:
                raise KeyError(f"identifier type needs to be declared on app_config_config 'Position_{file_config['name']}' like ISIN/CUSIP. Missing for file : {file_config['name']}")

            df = _read_file_into_dataframe(file_input_path, file_config, cols, skip_rows)

            df = _apply_operations(df, file_config['operations'])

            df = df[file_config['secCol']].str.strip().to_frame(name='secCol')
            df['identifier_type'] = iden_type
            df = df.dropna().drop_duplicates(subset='secCol').reset_index(drop=True)

            if ind_output == 'yes':
                logger.info(f"{logger_prefix} Exporting filtered file content for cross-check file : {Path(file_input_path).stem}")
                df.to_csv(os.path.join(output_path, str(Path(file_input_path).stem)+'_extract.csv'), sep='|', index=False)

            logger.info(f"{logger_prefix} Creating security dataframe and appending securities from both CUSIP/ISIN")
            # append dataframe with new records for security master values
            security_df = concat_dataframes(df, security_df)

            logger.info(f"{logger_prefix} Creating pricing dataframe and appending securities from ISIN")
            if file_config['idenType'] == 'ISIN':
                pricing_df = concat_dataframes(df, pricing_df)

            logger.info(f"{logger_prefix} Drop duplicates on final security and pricing dataframe")
            security_df = security_df.dropna().drop_duplicates(subset='secCol').reset_index(drop=True)
            pricing_df = pricing_df.dropna().drop_duplicates(subset='secCol').reset_index(drop=True)
        else:
            logger.error(f"{logger_prefix} fileParams key not found for file : {file_config.get('name')}. Please check.")

    except Exception as e:
        raise RuntimeError(f"{logger_prefix} Error getting securities for {file_input_path}: {e}")

    return security_df, pricing_df


def main():

    try:
        print (f"Started {task_name} task for app {app_name}...")
        run_mode = str(dag_ref).split('__')[0].split('-')[1]

        # Get application business date 
        business_date = dbm.get_app_business_date(app_name)

        # Create a insert in ts_task_status table
        dbm.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'processing', 'processing extract')

        # Get task_instance from ts_task_statsu table
        ts_instance = dbm.get_task_instance(app_name, task_name, dag_ref)

        # set a global variable for logger_prefix
        global logger_prefix 
        logger_prefix = f"[{ts_instance}] [{node_name}]"

        logger.info(f"{logger_prefix} Started executing task : {task_name} for application : {app_name} with Dag Referene : {dag_ref}...")

        logger.info(f"{logger_prefix} Collecting path config for position files.")
        input_params = dbm.get_output_params(app_name, task_name, 'Bloomberg_securities_input_path')
        logger.info(f"{logger_prefix} Bloomberg_securities_input_path config params : {input_params}")

        input_path = input_params[0][1] if input_params[0][1] else None

        if not input_path:
            logger.error(f"{logger_prefix} Input path not exists for config : Bloomberg_securities_input_path on ts_task_output_params table. exiting..")
            raise FileNotFoundError("Input path not exists for config : Bloomberg_securities_input_path on ts_task_output_params table. exiting.")
        else:
            logger.info(f"{logger_prefix} Bloomberg Securities Input path : {input_path}")

        output_params = dbm.get_output_params(app_name, task_name, 'Bloomberg_securities_output_path')
        logger.info(f"{logger_prefix} Bloomberg_securities_output_path config params : {output_params}")

        output_path = output_params[0][1] if output_params[0][1] else None

        if not output_path:
            logger.error(f"{logger_prefix} Output path not exists for config : Bloomberg_securities_output_path on ts_task_output_params table. exiting..")
            raise FileNotFoundError("Output path not exists for config : Bloomberg_securities_output_path on ts_task_output_params table. exiting.")
        else:
            logger.info(f"{logger_prefix} Bloomberg Securities Output path : {output_path}")

        position_filenames=dbm.get_app_config_param(app_name, task_name, 'Bloomberg_filewait_lookup')
        logger.info(f"{logger_prefix} position_filenames : {position_filenames}")

        indfile_output=dbm.get_app_config_param(app_name, task_name, 'Bloomberg_extract_ind_output')
        ind_output=indfile_output.get('indOutput', 'no')
        logger.info(f"{logger_prefix} individual output : {ind_output}")

        filenames_list=[item['name'] for item in position_filenames['bloombergFileLookups']]
        logger.info(f"{logger_prefix} filenames_list : {filenames_list}")

        file_pattern=dbm.get_file_pattern(app_name, task_name)
        if file_pattern:
            logger.info(f"{logger_prefix} File Pattern for task : {task_name} - {file_pattern}")
        else:
            raise ValueError(f"No file pattern found for task : {task_name}.")

        # initialize a dictionary to store the final results
        logger.info(f"{logger_prefix} Creating empty dataframe.")
        security_df = pd.DataFrame(columns=['secCol', 'identifier_type'])
        pricing_df = pd.DataFrame(columns=['secCol', 'identifier_type'])

        for file_name in filenames_list:
            logger.info(f"{logger_prefix} file_name : {file_name}")
            # get config data for particular file
            rec=dbm.get_app_config_param(app_name, task_name, 'Position_'+file_name)
            file_config=rec.get('posFileColumns')

            # form a path based on the srcSys configuration for each file
            file_path=os.path.join(input_path, str(file_config['srcSys']))

            file_names_list = file_pattern.get('Position_'+file_name)

            #replace the business_date in the file pattern
            file_name = str(file_names_list[0]).replace('{currdate}', bd.convert_to_date_format(business_date[1],file_names_list[1])).replace('{prevdate}', bd.convert_to_date_format(business_date[0],file_names_list[1])).replace('{nextdate}', bd.convert_to_date_format(business_date[2],file_names_list[1])).replace('{nextcalendardate}', bd.convert_to_date_format(business_date[1], file_names_list[1], 1))

            print (f"file_name : {file_name}")

            logger.info(f"{logger_prefix} File to search : {file_name} in directory : {file_path}")
            # change to feed path as per the srcSys configuration according to requirement
            files=get_files(file_path, str(file_name))
            logger.info(f"{logger_prefix} files : {files}")

            if files:
                if len(files) > 1:
                    logger.error(f"{logger_prefix} Duplicate file exists. {files} in directory: {file_path}. exiting.")
                    raise LookupError(f"Duplicate file exists. {files} in directory: {file_path}.")
                elif len(files) == 1:
                    logger.info(f"{logger_prefix} File found : {files}")
                    file_input_path = files[0]

                    logger.info(f"{logger_prefix} Input file path : {file_input_path}")

                    security_df, pricing_df = _process_single_file(
                        file_input_path, file_config, ind_output,
                        output_path, security_df, pricing_df
                    )
                else:
                    logger.warning(f"{logger_prefix} No matched files found for file config : {str(file_config['name'])}.")
            else:
                logger.warning(f"{logger_prefix} No files matching for file: {file_name} in directory: {file_path}")
                continue

        logger.info(f"{logger_prefix} Exporting dataframe of securities into excel")


        # inserting securites for security master
        update_extracted_securities('securityMaster', ts_instance, business_date[1], security_df)

        # inserting securities for pricing list
        update_extracted_securities('pricingData', ts_instance, business_date[1], pricing_df)

        # mark completion status for getSecurities task
        dbm.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'processing', 'security and pricing extract completed')

        if ind_output == 'yes':
            logger.info(f"{logger_prefix} Extracting final output containing unique securities of CUSIP and ISIN.")
            security_df.to_csv(os.path.join(output_path, f"security_master_final_extract_{str(business_date[1])}.csv"), sep='|', index=False)
            pricing_df.to_csv(os.path.join(output_path, f"pricing_list_final_extract_{str(business_date[1])}.csv"), sep='|', index=False)

        logger.info(f"{logger_prefix} Completed executing task : {task_name} for application : {app_name}")
    except Exception as exp:
        logger.error(f"{logger_prefix} Exception occurred : {exp}")
        dbm.update_ts_task_status(app_name, task_name, business_date[1], dag_ref, run_mode, 'error', 'error')
        sys.exit(1)


if __name__ == "__main__":
    main()