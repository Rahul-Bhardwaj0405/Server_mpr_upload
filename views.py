


# Create your views here.
from django.shortcuts import render, redirect
from django.http import JsonResponse
from .forms import UploadFileForm
from .tasks import process_uploaded_files
import logging
from django.shortcuts import render
from django.core.cache import cache
import oracledb
import tempfile
import os
import logging


# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger(__name__)



# def upload_files(request):
#     if request.method == 'POST':
#         form = UploadFileForm(request.POST, request.FILES)
#         if form.is_valid():
#             bank_name = form.cleaned_data['bank_name']
#             merchant_name = form.cleaned_data['merchant_name']
#             transaction_type = form.cleaned_data['transaction_type']
#             files = request.FILES.getlist('file')
            
#             # Prepare a list to hold the file contents and their formats
#             file_contents = []
#             file_formats = []  # This will hold the format of each file

#             for file in files:
#                 if file.size == 0:  # Check for empty files
#                     logger.error(f"File {file.name} is empty.")
#                     return JsonResponse({'message': f'File {file.name} is empty.'}, status=400)

#                 file_content = file.read()
#                 file_name = file.name
                
#                 logger.debug(f"Processing file: {file_name} with size {file.size} bytes.")

#                 # Determine file format based on extension
#                 if file_name.endswith('.xlsx') or file_name.endswith('.xls') or file_name.endswith('.ods'):
#                     file_format = 'excel'
#                 elif file_name.endswith('.csv'):
#                     file_format = 'csv'
#                 else:
#                     logger.error(f"Unsupported file extension for file: {file_name}")
#                     return JsonResponse({'message': 'Unsupported file extension. Only .xlsx, .xls, .ods, and .csv files are allowed.'}, status=400)

#                 # Store file content along with its name and format
#                 file_contents.append((file_content, file_name))  # Append the content and file name as a tuple
#                 file_formats.append(file_format)  # Append the format of the corresponding file

#             # Ensure that file_format is set
#             if not file_formats:
#                 logger.error("Could not determine file format for uploaded files.")
#                 return JsonResponse({'message': 'Unable to determine file format. Please upload .xlsx or .csv files or .xls files or .ods files.'}, status=400)

#             # Pass the full list of file contents, their names, bank name, transaction type, and formats to the task
#             process_uploaded_files.delay(file_contents, bank_name, transaction_type, file_formats, merchant_name)

#             return JsonResponse({'message': 'Files uploaded successfully. Processing started.'}, status=202)
#     else:
#         form = UploadFileForm()
    
#     return render(request, 'upload.html', {'form': form})


def upload_files(request):
    if request.method == 'POST':
        form = UploadFileForm(request.POST, request.FILES)
        if form.is_valid():
            bank_name = form.cleaned_data['bank_name']
            merchant_name = form.cleaned_data['merchant_name']
            transaction_type = form.cleaned_data['transaction_type']
            files = request.FILES.getlist('file')
            
            temp_file_paths = []
            file_formats = []

            for file in files:
                if file.size == 0:
                    logger.error(f"File {file.name} is empty.")
                    return JsonResponse({'message': f'File {file.name} is empty.'}, status=400)

                # Create a temporary file with the correct extension
                file_extension = file.name.lower().split('.')[-1]
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{file_extension}')
                for chunk in file.chunks():
                    temp_file.write(chunk)
                temp_file.close()

                # Determine file format based on extension
                if file_extension in ['xlsx', 'xls', 'ods']:
                    file_formats.append('excel')
                else:
                    file_formats.append('csv')

                temp_file_paths.append(temp_file.name)

            # Pass the original file format list (file_formats) and other necessary info to Celery task
            process_uploaded_files.delay(temp_file_paths, bank_name, transaction_type, file_formats, merchant_name)
            return JsonResponse({'message': 'Processing started.'}, status=202)
    else:
        form = UploadFileForm()

    return render(request, 'upload.html', {'form': form})


# def upload_files(request):
#     if request.method == 'POST':
#         form = UploadFileForm(request.POST, request.FILES)
#         if form.is_valid():
#             bank_name = form.cleaned_data['bank_name']
#             merchant_name = form.cleaned_data['merchant_name']
#             transaction_type = form.cleaned_data['transaction_type']
#             files = request.FILES.getlist('file')
            
#             temp_file_paths = []
#             file_formats = []

#             for file in files:
#                 if file.size == 0:
#                     logger.error(f"File {file.name} is empty.")
#                     return JsonResponse({'message': f'File {file.name} is empty.'}, status=400)

#                 temp_file = tempfile.NamedTemporaryFile(delete=False)
#                 for chunk in file.chunks():
#                     temp_file.write(chunk)
#                 temp_file.close()

#                 # file_formats.append('excel' if file.name.endswith(('.xlsx', '.xls', '.ods')) else 'csv')
#                 file_formats.append('excel' if file.name.lower().endswith(('.xlsx', '.xls', '.ods')) else 'csv')
#                 temp_file_paths.append(temp_file.name)

#             process_uploaded_files.delay(temp_file_paths, bank_name, transaction_type, file_formats, merchant_name)
#             return JsonResponse({'message': 'Files uploaded successfully. Processing started.'}, status=202)
#     else:
#         form = UploadFileForm()
    
#     return render(request, 'upload.html', {'form': form})



def transaction_results_view(request):
    # Retrieve the results from the cache
    results = cache.get('latest_transaction_results')
    if results is None:
        logger.error("No cached results found.")
        results = {
            "total_successful": 0,
            "total_failed": 0,
        }
    else:
        logger.debug(f"Retrieved results from cache: {results}")

    context = {
        'total_successful': results['total_successful'],
        'total_failed': results['total_failed'],
    }

    return render(request, 'transaction_results.html', context)


# import cx_Oracle

# dsn = cx_Oracle.makedsn("10.78.14.42", 1725, service_name="rptdb_srv.cris.org.in")

# try:
#     with cx_Oracle.connect(user="PGACT7", password="Nov2024", dsn=dsn) as connection:
#         with connection.cursor() as cursor:
#             # Use FETCH FIRST to limit the rows (Oracle SQL syntax)
#             sql = """SELECT * FROM TRANSACTION_DB.ET_TICKET FETCH FIRST 10 ROWS ONLY"""
#             # Execute the query
#             cursor.execute(sql)
#             # Fetch and print the results
#             for row in cursor:
#                 print(row)
# except cx_Oracle.DatabaseError as e:
#     print("An error occurred:", e)




# Establish a connection to the production Oracle database
with oracledb.connect(user='PGACT7', password='Nov2024', dsn='10.78.14.42:1725/rptdb_srv.cris.org.in') as connection:
    with connection.cursor() as cursor:
        # Use FETCH FIRST to limit the rows (Oracle SQL syntax)
        sql = """SELECT * FROM TRANSACTION_DB.ET_TICKET FETCH FIRST 10 ROWS ONLY"""

        # Execute the query
        cursor.execute(sql)
        # Fetch and print the results
        for row in cursor:
            print(row)


# def transaction_list(request):
#     transactions = TransactionData.objects.all().order_by('-date')
#     paginator = Paginator(transactions, 20)  # Show 20 transactions per page
#     page_number = request.GET.get('page')
#     page_obj = paginator.get_page(page_number)
    
#     total_amount = TransactionData.objects.aggregate(Sum('amount'))['amount__sum']
    
#     context = {
#         'page_obj': page_obj,
#         'total_amount': total_amount,
#     }
#     return render(request, 'transaction_list.html', context)








# import oracledb

# # Establish a connection to the production Oracle database
# with oracledb.connect(user='PGACT7', password='Oct2024', dsn='10.78.14.42:1725/rptdb_srv.cris.org.in') as connection:
#     with connection.cursor() as cursor:
#         # Use FETCH FIRST to limit the rows (Oracle SQL syntax)
#         sql = """SELECT "PAYMENT_DATE", "ENTITY_ID", "AMOUNT", "BANK_ID" 
#                  FROM TRANSACTION_DB.ET_PAYMENT_CASH 
#                  WHERE "BANK_ID" = 40 AND ROWNUM <= 2"""

#         # Execute the query
#         cursor.execute(sql)
#         # Fetch and print the results
#         for row in cursor:
#             print(row)