import pandas as pd
import logging
from celery import shared_task
from .models import Transaction
from io import BytesIO
import csv
import re
from django.core.cache import cache
from django.db import transaction
import os


# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format=(
        '%(asctime)s - %(levelname)s - %(name)s - %(module)s - '
        '%(funcName)s - line:%(lineno)d - %(process)d - '
        '%(threadName)s - %(message)s'
    ),
    handlers=(
        logging.StreamHandler(),
    )
)

logger = logging.getLogger(__name__)

BANK_CODE_MAPPING = {
    'hdfc': 101,
    'icici': 102,
    'karur_vysya': 40,
}

BANK_MAPPINGS = {
    'karur_vysya': {
        'booking': {
            'columns': ['TXN DATE', 'IRCTC ORDER NO.', 'BANK BOOKING REF.NO.', 'BOOKING AMOUNT', 'CREDITED ON'],
            'column_mapping': {
                'IRCTCORDERNO': 'Order_Id',
                'BANKBOOKINGREFNO': 'Bank_Ref_id',
                'BOOKINGAMOUNT': 'Payable_Merchant',
                'TXNDATE': 'Transaction_Date',
                'CREDITEDON': 'Settlement_Date'
            }
        },
        'refund': {
            'columns': ['REFUND DATE', 'IRCTC ORDER NO.', 'BANK BOOKING REF.NO.', 'BANK REFUND REF.NO.', 'REFUND AMOUNT', 'DEBITED ON'],
            'column_mapping': {
                'IRCTCORDERNO': 'Order_Id',
                'REFUNDAMOUNT': 'Payable_Merchant',
                'DEBITEDON': 'Settlement_Date',
                'REFUNDDATE': 'Transaction_Date',
                'BANKBOOKINGREFNO': 'Bank_Ref_id',
                'BANKREFUNDREFNO': 'Refund_Order_Id'
            }
        }
    },
    'icici': {
        'both': {
            'columns': ['POST DATE', 'FT NO.', 'SESSION ID [ASPD]', 'ARN NO', 'MID', 'TRANSACTION DATE',  'NET AMT', 'CARD NUMBER', 'CARD TYPE', 'TID'],
            'column_mapping': {
                'TRANSACTIONDATE': 'Transaction_Date',
                'SESSIONID': 'Order_Id',
                'FTNO': 'Transaction_Id',
                'ARNNO': 'Arn_No',
                'MID': 'MID',
                'POSTDATE': 'Settlement_Date',
                'NETAMT': 'Payable_Merchant',
                'CARDNUMBER': 'Card_No',
                'CARDTYPE': 'Card_type',
                'TID': 'Tid'
            }
        }
    },
}

# Precompile regex pattern for performance
column_cleaning_regex = re.compile(r'\[.*?\]')

def clean_column_name(column_name):
    # logger.debug(f"Cleaning column name: '{column_name}'")
    cleaned_name = column_cleaning_regex.sub('', column_name)
    cleaned_name = ''.join(part for part in cleaned_name.split() if part)
    cleaned_name = ''.join(char for char in cleaned_name if char not in ['.', '_']).strip()
    # logger.debug(f"Cleaned column name: '{cleaned_name}'")
    return cleaned_name

def convert_payable_to_float(df, column_name):
    # logger.debug(f"Converting column '{column_name}' to float.")
    df[column_name] = pd.to_numeric(df[column_name].str.replace(',', ''), errors='coerce')
    return df

def convert_column_to_datetime(df, column_name):
    # logger.debug(f"Converting column '{column_name}' to datetime.")
    df[column_name] = pd.to_datetime(df[column_name], errors='coerce')
    
    # Log details of rows where datetime parsing resulted in NaT
    unsuccessful_dates = df[df[column_name].isna()]
    if not unsuccessful_dates.empty:
        logger.warning(f"Could not parse dates in column '{column_name}'; rows affected: {unsuccessful_dates.index.tolist()}")
        # df[column_name].fillna(value=None, inplace=True)  # or specify default date here
    return df

def handle_nat_in_datetime_fields(transaction_data):
    # List all the datetime fields expected in transaction_data
    datetime_fields = ['Transaction_Date', 'Settlement_Date', 'Refund_Request_Date', 'Credit_Debit_Date', 'File_upload_Date']
    
    for field in datetime_fields:
        if transaction_data[field] is pd.NaT:
            # Log the NaT issue with specific data point reference
            logger.warning(f"Encountered NaT in {field}, setting to None or default for transaction ID: {transaction_data.get('Transaction_Id', 'Unknown')}")
            # Assign None or a specific default date as necessary
            transaction_data[field] = None

@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 5, 'countdown': 60})
def process_uploaded_files(self, file_paths, bank_name, transaction_type, file_formats, merchant_name):
    logger.info(f"Starting process_uploaded_files with bank_name: {bank_name}, transaction_type: {transaction_type}, file_formats: {file_formats}, merchant_name: {merchant_name}")
    
    try:
        total_files = len(file_paths)
        logger.info(f"Total number of files to process: {total_files}")

        for file_index, file_path in enumerate(file_paths):
            file_name = os.path.basename(file_path)
            file_extension = os.path.splitext(file_name)[-1].lower()  # Get file extension
            
            logger.info(f"Processing file: {file_name} of type {transaction_type}")
            df_chunks = []

            try:
                # Determine the file format
                file_format = file_formats[file_index]
                if file_format == 'excel':
                    # Ensure file extension is valid
                    if file_extension in ('.xlsx', '.xls', '.ods'):
                        # Proceed with processing the Excel file
                        if file_extension == '.xlsx':
                            df = pd.read_excel(file_path, dtype=str, engine='openpyxl')
                        elif file_extension == '.xls':
                            df = pd.read_excel(file_path, dtype=str, engine='xlrd')
                        elif file_extension == '.ods':
                            df = pd.read_excel(file_path, dtype=str, engine='odf')
                        df_chunks = [df]  # For consistency in chunked processing
                    else:
                        logger.error(f"Unsupported Excel format for file: {file_name}, extension: {file_extension}")
                        continue

                elif file_format == 'csv':
                    df_chunks = pd.read_csv(file_path, dtype=str, quotechar='"', quoting=csv.QUOTE_MINIMAL, chunksize=50000)
                    logger.info(f"Successfully read CSV file {file_name} in chunks.")
                else:
                    logger.error(f"Unsupported file format: {file_format} for file: {file_name}")
                    continue

                # Process each chunk
                for chunk_index, df_chunk in enumerate(df_chunks):
                    process_dataframe_chunk(df_chunk, bank_name, transaction_type, merchant_name)

            except Exception as e:
                logger.error(f"Error processing chunk from file: {file_name}. Exception: {e}", exc_info=True)
                continue
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)  # Ensure file is deleted after processing
                    logger.info(f"Deleted temporary file: {file_path}")
                else:
                    logger.warning(f"File not found for deletion: {file_path}")
                # os.remove(file_path)  # Ensure file is deleted after processing
                # logger.info(f"Deleted temporary file: {file_path}")

            logger.info(f"File processing completed for {file_name}")

        logger.info("All files have been processed.")

    except Exception as e:
        logger.error(f"Error in processing files. Exception: {e}", exc_info=True)
        raise


# def convert_column_to_datetime(df, column_name):
#     logger.debug(f"Converting column '{column_name}' to datetime.")
#     df[column_name] = pd.to_datetime(df[column_name], errors='coerce')
#     return df

# def process_uploaded_files(file_contents, bank_name, transaction_type, file_formats, merchant_name):
#     logger.info(f"Starting process_uploaded_files with bank_name: {bank_name}, transaction_type: {transaction_type}, file_formats: {file_formats}, merchant_name: {merchant_name}")
    
#     try:
#         total_files = len(file_contents)
#         logger.info(f"Total number of files to process: {total_files}")

#         for file_index, (file_content, file_name) in enumerate(file_contents):
#             logger.info(f"Processing file: {file_name} of type {transaction_type}")
#             df_chunks = []

#             try:
#                 if file_formats[file_index] == 'excel':
#                     # logger.debug(f"Reading file {file_name} as Excel.")
#                     if file_name.endswith('.xlsx'):
#                         # logger.debug(f"Detected Excel file format: .xlsx")
#                         df = pd.read_excel(BytesIO(file_content), dtype=str, engine='openpyxl')
#                         df_chunks = [df]
#                     elif file_name.endswith('.xls'):
#                         # logger.debug(f"Detected Excel file format: .xls")
#                         df = pd.read_excel(BytesIO(file_content), dtype=str, engine='xlrd')
#                         df_chunks = [df]
#                     elif file_name.endswith('.ods'):
#                         # logger.debug(f"Detected Ods file format: .ods")
#                         df = pd.read_excel(BytesIO(file_content), dtype=str, engine='odf')
#                         df_chunks = [df]
#                     else:
#                         logger.error(f"File {file_name} does not have a valid Excel extension.")
#                 elif file_formats[file_index] == 'csv':
#                     logger.debug(f"Reading file {file_name} as CSV.")
#                     df_chunks = pd.read_csv(BytesIO(file_content), dtype=str, quotechar='"', quoting=csv.QUOTE_MINIMAL, chunksize=30000)
#                     logger.info(f"Successfully read CSV file {file_name} in chunks.")
#                 else:
#                     logger.error(f"Unsupported file format: {file_formats[file_index]} for {file_name}")
#                     raise ValueError("Unsupported file format")

#                 for chunk_index, df_chunk in enumerate(df_chunks):
#                     # logger.debug(f"Processing chunk {chunk_index + 1} of file {file_name} with shape: {df_chunk.shape}")
#                     process_dataframe_chunk(df_chunk, bank_name, transaction_type, merchant_name)

#             except Exception as e:
#                 logger.error(f"Error processing chunk from file: {file_name}. Exception: {e}", exc_info=True)
#                 continue

#             logger.info(f"File processing completed for {file_name}")

#         logger.info("All files have been processed.")

#     except Exception as e:
#         logger.error(f"Error processing files. Exception: {e}", exc_info=True)
#         raise


# @shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 5, 'countdown': 60})
# def process_uploaded_files(self, file_paths, bank_name, transaction_type, file_formats, merchant_name):
#     logger.info(f"Starting process_uploaded_files with bank_name: {bank_name}, transaction_type: {transaction_type}, file_formats: {file_formats}, merchant_name: {merchant_name}")
    
#     try:
#         total_files = len(file_paths)
#         logger.info(f"Total number of files to process: {total_files}")

#         for file_index, file_path in enumerate(file_paths):
#             file_name = os.path.basename(file_path)
#             logger.info(f"Processing file: {file_name} of type {transaction_type}")
#             df_chunks = []

#             try:
#                 # Determine the file format
#                 file_format = file_formats[file_index]
#                 if file_format == 'excel':
#                     # Handling various Excel formats
#                     if file_path.endswith('.xlsx'):
#                         df = pd.read_excel(file_path, dtype=str, engine='openpyxl')
#                     elif file_path.endswith('.xls'):
#                         df = pd.read_excel(file_path, dtype=str, engine='xlrd')
#                     elif file_path.endswith('.ods'):
#                         df = pd.read_excel(file_path, dtype=str, engine='odf')
#                     else:
#                         # logger.error(f"Unsupported Excel format for file: {file_name}")
#                         logger.error(f"Unsupported Excel format for file: {file_name}, extension: {file_path[-4:]}")
#                         continue
#                     df_chunks = [df]  # For consistency in chunked processing

#                 elif file_format == 'csv':
#                     df_chunks = pd.read_csv(file_path, dtype=str, quotechar='"', quoting=csv.QUOTE_MINIMAL, chunksize=30000)
#                     logger.info(f"Successfully read CSV file {file_name} in chunks.")
#                 else:
#                     # logger.error(f"Unsupported file format: {file_format} for file: {file_name}")
#                     logger.error(f"Unsupported Excel format for file: {file_name}, extension: {file_path[-4:]}")
#                     continue

#                 # Process each chunk
#                 for chunk_index, df_chunk in enumerate(df_chunks):
#                     process_dataframe_chunk(df_chunk, bank_name, transaction_type, merchant_name)

#             except Exception as e:
#                 logger.error(f"Error processing chunk from file: {file_name}. Exception: {e}", exc_info=True)
#                 continue
#             finally:
#                 os.remove(file_path)  # Ensure file is deleted after processing
#                 logger.info(f"Deleted temporary file: {file_path}")

#             logger.info(f"File processing completed for {file_name}")

#         logger.info("All files have been processed.")

#     except Exception as e:
#         logger.error(f"Error in processing files. Exception: {e}", exc_info=True)
#         raise

# @shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 5, 'countdown': 60})
# def process_uploaded_files(self, file_paths, bank_name, transaction_type, file_formats, merchant_name):
#     logger.info(f"Starting process_uploaded_files with bank_name: {bank_name}, transaction_type: {transaction_type}, file_formats: {file_formats}, merchant_name: {merchant_name}")
    
#     try:
#         total_files = len(file_paths)
#         logger.info(f"Total number of files to process: {total_files}")

#         for file_index, file_path in enumerate(file_paths):
#             file_name = os.path.basename(file_path)
#             logger.info(f"Processing file: {file_name} of type {transaction_type}")
#             df_chunks = []

#             try:
#                 # Determine the file format
#                 file_format = file_formats[file_index]
#                 if file_format == 'excel':
#                     # Make sure the file extension is valid
#                     if file_name.lower().endswith(('.xlsx', '.xls', '.ods')):
#                         # Proceed with processing Excel file
#                         if file_name.endswith('.xlsx'):
#                             df = pd.read_excel(file_path, dtype=str, engine='openpyxl')
#                         elif file_name.endswith('.xls'):
#                             df = pd.read_excel(file_path, dtype=str, engine='xlrd')
#                         elif file_name.endswith('.ods'):
#                             df = pd.read_excel(file_path, dtype=str, engine='odf')
#                         df_chunks = [df]  # For consistency in chunked processing
#                     else:
#                         logger.error(f"Unsupported Excel format for file: {file_name}, extension: {file_name.split('.')[-1]}")
#                         continue

#                 elif file_format == 'csv':
#                     df_chunks = pd.read_csv(file_path, dtype=str, quotechar='"', quoting=csv.QUOTE_MINIMAL, chunksize=30000)
#                     logger.info(f"Successfully read CSV file {file_name} in chunks.")
#                 else:
#                     logger.error(f"Unsupported file format: {file_format} for file: {file_name}")
#                     continue

#                 # Process each chunk
#                 for chunk_index, df_chunk in enumerate(df_chunks):
#                     process_dataframe_chunk(df_chunk, bank_name, transaction_type, merchant_name)

#             except Exception as e:
#                 logger.error(f"Error processing chunk from file: {file_name}. Exception: {e}", exc_info=True)
#                 continue
#             finally:
#                 os.remove(file_path)  # Ensure file is deleted after processing
#                 logger.info(f"Deleted temporary file: {file_path}")

#             logger.info(f"File processing completed for {file_name}")

#         logger.info("All files have been processed.")

#     except Exception as e:
#         logger.error(f"Error in processing files. Exception: {e}", exc_info=True)
#         raise


def process_dataframe_chunk(df_chunk, bank_name, transaction_type, merchant_name):
    logger.info(f"Processing dataframe chunk for bank: {bank_name}, transaction type: {transaction_type}, merchant_name: {merchant_name}")
    cleaned_columns = [clean_column_name(col) for col in df_chunk.columns]
    df_chunk.columns = cleaned_columns
    # logger.debug(f"Cleaned DataFrame columns: {df_chunk.columns.tolist()}")

    bank_mapping = BANK_MAPPINGS.get(bank_name, {})

    if transaction_type in bank_mapping:
        columns_mapping = bank_mapping[transaction_type]
    elif 'both' in bank_mapping:
        columns_mapping = bank_mapping['both']
    else:
        logger.error(f"No valid mapping found for bank: {bank_name} and transaction type: {transaction_type}")
        return

    column_mapping = columns_mapping.get('column_mapping', {})
    expected_columns = [clean_column_name(col) for col in columns_mapping.get('columns', [])]

    if all(col in df_chunk.columns for col in expected_columns):
        df_chunk.rename(columns=column_mapping, inplace=True)
        # logger.debug(f"Renamed columns: {df_chunk.columns.tolist()}")

        if 'Transaction_Date' in df_chunk.columns:
            df_chunk = convert_column_to_datetime(df_chunk, 'Transaction_Date')
        if 'Settlement_Date' in df_chunk.columns:
            df_chunk = convert_column_to_datetime(df_chunk, 'Settlement_Date')
        if 'Payable_Merchant' in df_chunk.columns:
            df_chunk = convert_payable_to_float(df_chunk, 'Payable_Merchant')
            
            if bank_name == 'icici':
                df_chunk['Payable_Merchant'] = df_chunk['Payable_Merchant'].fillna(0)  # Assuming 0 is a neutral value for your logic
                df_chunk['CREDIT_DEBIT_AMOUNT'] = df_chunk['Payable_Merchant'].apply(
                    lambda x: 'CREDIT' if x > 0 else 'DEBIT' if x < 0 else None
                )
                # logger.debug(f"Populated CREDIT/DEBIT AMOUNT column: {df_chunk[['Payable_Merchant', 'CREDIT_DEBIT_AMOUNT']]}")

        process_transactions(df_chunk, bank_name, transaction_type, merchant_name)
    else:
        missing_cols = set(expected_columns) - set(df_chunk.columns)
        logger.error(f"Missing expected columns: {missing_cols} for bank: {bank_name}, type: {transaction_type}")

def process_transactions(df_chunk, bank_name, transaction_type, merchant_name):
    logger.info(f"Started processing transactions for bank: {bank_name}, type: {transaction_type}, merchant_name: {merchant_name}")
    bulk_data_transactions = []
    seen_orders = set()
    bank_id = BANK_CODE_MAPPING.get(bank_name, None)

    if bank_id is None:
        logger.error(f"No bank ID found for bank: {bank_name}")
        return
    
    banks_with_mid_override = ['hdfc', 'icici', 'indus']

    success_count = 0
    fail_count = 0

    for index, row in df_chunk.iterrows():
        
        if transaction_type not in ['booking', 'refund', 'both']:
            logger.error(f"Unexpected transaction type: {transaction_type}. Skipping.")
            fail_count += 1
            continue

        transaction_data = {
            'Transaction_type': transaction_type,
            'Merchant_Name': merchant_name,
            'MID': row.get('MID') if bank_name in banks_with_mid_override else bank_id,
            'Transaction_Id': row.get('Transaction_Id'),
            'Order_Id': row.get('Order_Id'),
            'Transaction_Date': row.get('Transaction_Date'),
            'Settlement_Date': row.get('Settlement_Date'),
            'Refund_Request_Date': convert_column_to_datetime(df_chunk, 'Refund_Request_Date') if 'Refund_Request_Date' in df_chunk else None,
            'Gross_Amount': convert_payable_to_float(df_chunk, 'Gross_Amount') if 'Gross_Amount' in df_chunk else None,
            'Aggregator_Com': convert_payable_to_float(df_chunk, 'Aggregator_Com') if 'Aggregator_Com' in df_chunk else None,
            'Acquirer_Comm': convert_payable_to_float(df_chunk, 'Acquirer_Comm') if 'Acquirer_Comm' in df_chunk else None,
            'Payable_Merchant': row.get('Payable_Merchant'),
            'Payout_from_Nodal': convert_payable_to_float(df_chunk, 'Payout_from_Nodal') if 'Payout_from_Nodal' in df_chunk else None,
            'BankName_Receive_Funds': row.get('BankName_Receive_Funds'),
            'Nodal_Account_No': row.get('Nodal_Account_No'),
            'Aggregator_Name': row.get('Aggregator_Name'),
            'Acquirer_Name': row.get('Acquirer_Name'),
            'Refund_Flag': row.get('Refund_Flag'),
            'Payments_Type': row.get('Payments_Type'),
            'MOP_Type': row.get('MOP_Type'),
            'Credit_Debit_Date': convert_column_to_datetime(df_chunk, 'Credit_Debit_Date') if 'Credit_Debit_Date' in df_chunk else None,
            'Bank_Name': bank_name,
            'Refund_Order_Id': row.get('Refund_Order_Id'),
            'Acq_Id': row.get('Acq_Id'),
            'Approve_code': row.get('Approve_code'),
            'Arn_No': row.get('Arn_No'),
            'Card_No': row.get('Card_No'),
            'Tid': row.get('Tid'),
            'Remarks': row.get('Remarks'),
            'Bank_Ref_id': row.get('Bank_Ref_id'),
            'File_upload_Date': convert_column_to_datetime(df_chunk, 'File_upload_Date') if 'File_upload_Date' in df_chunk else None,
            'User_name': row.get('User_name'),
            'Recon_Status': row.get('Recon_Status'),
            'Mpr_Summary_Trans': row.get('Mpr_Summary_Trans'),
            'Merchant_code': row.get('Merchant_code'),
            'Rec_Fmt': row.get('Rec_Fmt'),
            'Card_type': row.get('Card_type'),
            'Intl_Amount': convert_payable_to_float(df_chunk, 'Intl_Amount') if 'Intl_Amount' in df_chunk else None,
            'Domestic_Amount': convert_payable_to_float(df_chunk, 'Domestic_Amount') if 'Domestic_Amount' in df_chunk else None,
            'UDF1': None,
            'UDF2': None,
            'UDF3': None,
            'UDF4': None,
            'UDF5': None,
            'UDF6': None,
            'GST_Number': row.get('GST_Number'),
            'Credit_Debit_Amount': row.get('CREDIT_DEBIT_AMOUNT'),
        }

        handle_nat_in_datetime_fields(transaction_data)


        bulk_data_transactions.append(transaction_data)
        success_count += 1

    # if bulk_data_transactions:
    #     logger.debug(f"Bulk inserting {len(bulk_data_transactions)} transactions.")
    #     try:
    #         Transaction.bulk_create_transactions(bulk_data_transactions)
    #         # logger.info(f"Processed {len(bulk_data_transactions)} transactions successfully.")
    #     except Exception as e:
    #         logger.error(f"Transaction failed: {e}", exc_info=True)
    #         fail_count += len(bulk_data_transactions)

    try:
        with transaction.atomic():
            Transaction.bulk_create_transactions(bulk_data_transactions)
            # logger.info(f"Processed {len(bulk_data_transactions)} transactions successfully.")
        # except ValueError as ve:
        #     logger.error(f"ValueError during bulk creation, possibly due to NaT issues: {ve}")
    except Exception as e:
        logger.error(f"Transaction failed: {e}", exc_info=True)
            # logger.error(f"Transaction failed: {e}. Problematic Data: {bulk_data_transactions}", exc_info=True)
        fail_count += len(bulk_data_transactions)

    logger.debug(f"Batch processing complete. Successful: {success_count}, Failed: {fail_count}")


    # Cache the results for later retrieval
    results = {
        "total_successful": success_count,
        "total_failed": fail_count,
    }
    cache.set('latest_transaction_results', results, timeout=3600) 

    return results



