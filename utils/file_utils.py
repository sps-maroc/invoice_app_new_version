import os
import re
import shutil
import logging
from datetime import datetime
import PyPDF2

# Setup logging
log = logging.getLogger(__name__)

def sanitize_filename(filename):
    """
    Custom function to sanitize filename while preserving spaces and parentheses.
    This avoids the double underscore problem with secure_filename.
    """
    # Remove any dangerous characters
    filename = re.sub(r'[^\w\s\(\)\.\-€]', '_', filename)
    
    # Replace consecutive spaces with a single space
    filename = re.sub(r'\s+', ' ', filename)
    
    # Ensure the filename doesn't start or end with spaces
    filename = filename.strip()
    
    # If the filename is empty after sanitization, use a default name
    if not filename:
        filename = "invoice.pdf"
    
    return filename

def allowed_file(filename):
    """Check if the file has an allowed extension"""
    allowed_extensions = {'pdf'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

def cleanup_uploaded_files(file_path, keep_preview=True, preview_folder=None):
    """Clean up uploaded files but optionally keep the preview file"""
    try:
        # Always try to remove the original uploaded file
        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)
            log.info(f"Removed original file: {file_path}")
            
        # If keep_preview is False, also remove the preview file
        if not keep_preview and preview_folder:
            filename = os.path.basename(file_path)
            preview_path = os.path.join(preview_folder, filename)
            if os.path.exists(preview_path) and os.path.isfile(preview_path):
                os.remove(preview_path)
                log.info(f"Removed preview file: {preview_path}")
                
        return True
    except Exception as e:
        log.error(f"Error cleaning up files: {str(e)}")
        return False

def organize_file(file_path, archive_dir):
    """Organize a file by copying it to the archive directory with a structured path"""
    if not os.path.exists(file_path):
        return None
        
    # Create organized file path: YYYY/MM/invoice_filename.pdf
    now = datetime.now()
    year_dir = os.path.join(archive_dir, str(now.year))
    month_dir = os.path.join(year_dir, f"{now.month:02d}")
    
    # Create directories if they don't exist
    os.makedirs(month_dir, exist_ok=True)
    
    # Get the filename
    filename = os.path.basename(file_path)
    
    # Create organized path
    organized_path = os.path.join(month_dir, filename)
    
    # Copy the file
    try:
        shutil.copy2(file_path, organized_path)
        return organized_path
    except Exception as e:
        log.error(f"Error organizing file: {str(e)}")
        return None

def cleanup_processed_files(original_path, organized_path, upload_folder=None, preview_folder=None):
    """Clean up processed files and empty directories"""
    try:
        # Check if the files are different before deleting original
        if original_path and organized_path and original_path != organized_path:
            # Only delete if the file exists and is not already deleted
            if os.path.exists(original_path):
                log.info(f"Removing original file: {original_path}")
                os.remove(original_path)
                
            # Try to remove empty parent directories in uploads folder
            if upload_folder:
                parent_dir = os.path.dirname(original_path)
                if parent_dir.startswith(upload_folder):
                    while parent_dir and parent_dir != upload_folder:
                        if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                            try:
                                os.rmdir(parent_dir)
                                log.info(f"Removed empty directory: {parent_dir}")
                                parent_dir = os.path.dirname(parent_dir)
                            except OSError:
                                # Directory not empty or other issue
                                break
                        else:
                            break
            
            # Also clean up preview files
            if preview_folder and original_path:
                filename = os.path.basename(original_path)
                preview_path = os.path.join(preview_folder, f"preview_{filename}")
                if os.path.exists(preview_path):
                    os.remove(preview_path)
                    log.info(f"Removed preview file: {preview_path}")
        
        return True
    except Exception as e:
        log.error(f"Error during file cleanup: {str(e)}")
        return False

def check_for_duplicate_invoice(file_path, check_invoice_exists_func, extract_invoice_data_func):
    """Check if an invoice is a duplicate by examining its content"""
    try:
        # Extract invoice number from file using OCR only, don't process or store anything
        data = extract_invoice_data_func(file_path, store_in_db=False)
        invoice_number = None
        
        # Try to find invoice number in different data structures
        if data:
            if 'Rechnungsnummer' in data:
                invoice_number = data.get('Rechnungsnummer')
            elif 'invoice_number' in data:
                invoice_number = data.get('invoice_number')
            elif 'invoice_data' in data and 'invoice_number' in data['invoice_data']:
                invoice_number = data['invoice_data']['invoice_number']
            
        if invoice_number and invoice_number not in ['Unknown', 'Not found', 'Nicht gefunden', '']:
            # Check if this invoice number exists
            is_duplicate = check_invoice_exists_func(invoice_number)
            if is_duplicate:
                return {
                    'is_duplicate': True,
                    'invoice_number': invoice_number,
                    'error': f"Invoice number {invoice_number} already exists in the database"
                }
        
        # No duplicate found by invoice number
        return {'is_duplicate': False, 'invoice_number': invoice_number}
        
    except Exception as e:
        log.error(f"Error checking for duplicate: {str(e)}")
        return {'is_duplicate': False}

def _parse_amount(amount_str):
    """Parse an amount string to float"""
    if not amount_str:
        return 0.0
        
    # If it's already a float or int, return it
    if isinstance(amount_str, (float, int)):
        return float(amount_str)
    
    # Clean the string
    try:
        # Remove currency symbols and other non-numeric chars except . and ,
        cleaned = re.sub(r'[^\d.,]', '', str(amount_str))
        
        # Handle different number formats
        if ',' in cleaned and '.' not in cleaned:
            cleaned = cleaned.replace(',', '.')
        elif ',' in cleaned and '.' in cleaned:
            cleaned = cleaned.replace('.', '').replace(',', '.')
            
        return float(cleaned)
    except Exception as e:
        log.error(f"Error parsing amount '{amount_str}': {e}")
        return 0.0 