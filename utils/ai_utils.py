import os
import logging
import requests
import PyPDF2
import time

# Setup logging
log = logging.getLogger(__name__)

def select_ai_model(file_path, file_size=None):
    """
    Select the appropriate AI model based on file size, complexity, and availability
    
    Args:
        file_path: Path to the invoice PDF file
        file_size: Optional file size in bytes (will be calculated if not provided)
        
    Returns:
        str: Name of the selected AI model
    """
    if file_size is None:
        file_size = os.path.getsize(file_path)
    
    # Size in MB
    size_mb = file_size / (1024 * 1024)
    log.info(f"Selecting model for file {os.path.basename(file_path)}, size: {size_mb:.2f} MB")
    
    # Default model - using llama3 consistently
    model = "llama3:latest"
    
    # Try to check file complexity to inform model selection
    complexity_score = estimate_file_complexity(file_path)
    
    # Try to get model info from Ollama
    try:
        OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://localhost:11434')
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        
        if response.status_code == 200:
            available_models = [m["name"] for m in response.json().get("models", [])]
            log.info(f"Available models: {available_models}")
            
            # For complex or larger files, prefer more powerful models
            if complexity_score > 0.7 or size_mb > 5:
                preferred_models = ["gemma2:latest", "llama3:latest", "llama3:8b", "mistral:latest"]
            elif complexity_score > 0.4 or size_mb > 2:
                preferred_models = ["llama3:latest", "mistral:latest", "gemma2:latest"]
            else:
                preferred_models = ["llama3:latest", "mistral:latest", "gemma2:latest"]
            
            for preferred in preferred_models:
                if preferred in available_models:
                    model = preferred
                    log.info(f"Selected model based on complexity ({complexity_score:.2f}) and size ({size_mb:.2f} MB): {model}")
                    break
    except Exception as e:
        log.error(f"Error checking available models: {e}")
        log.info(f"Falling back to default model: {model}")
    
    log.info(f"Final model selection: {model}")
    return model

def estimate_file_complexity(file_path):
    """
    Estimate the complexity of a PDF file to help with model selection
    Higher score = more complex document
    
    Args:
        file_path: Path to the PDF file
        
    Returns:
        float: Complexity score between 0 and 1
    """
    try:
        # Start with a base complexity
        complexity = 0.5
        
        # Check if file exists
        if not os.path.exists(file_path):
            return complexity
        
        with open(file_path, 'rb') as f:
            try:
                pdf_reader = PyPDF2.PdfReader(f)
                
                # Factor 1: Number of pages (more pages = more complex)
                num_pages = len(pdf_reader.pages)
                if num_pages > 10:
                    complexity += 0.2
                elif num_pages > 5:
                    complexity += 0.1
                
                # Factor 2: Text density 
                text_length = 0
                sample_pages = min(3, num_pages)  # Sample up to 3 pages
                
                for i in range(sample_pages):
                    page = pdf_reader.pages[i]
                    text = page.extract_text() or ""
                    text_length += len(text)
                
                avg_text_length = text_length / sample_pages
                if avg_text_length > 3000:  # Very dense text
                    complexity += 0.2
                elif avg_text_length > 1500:  # Moderate text
                    complexity += 0.1
                elif avg_text_length < 200:  # Very little text (might be scan or images)
                    complexity += 0.15  # Scanned docs can be trickier
                
                # Factor 3: File size per page (indicator of images, complex formatting)
                size_per_page = os.path.getsize(file_path) / (num_pages * 1024)  # KB per page
                if size_per_page > 500:  # Very large per page (likely many images/complex formatting)
                    complexity += 0.15
                elif size_per_page > 200:
                    complexity += 0.05
                
                # Cap complexity between 0 and 1
                complexity = max(0, min(1, complexity))
                
            except Exception as e:
                log.warning(f"Error analyzing PDF complexity: {e}")
    except Exception as e:
        log.error(f"Error estimating file complexity: {e}")
    
    return complexity

def extract_text_from_pdf(file_path):
    """Extract text from a PDF file using PyPDF2"""
    pdf_text = ""
    
    try:
        with open(file_path, 'rb') as f:
            try:
                pdf_reader = PyPDF2.PdfReader(f)
                for page_num in range(len(pdf_reader.pages)):
                    try:
                        extracted_text = pdf_reader.pages[page_num].extract_text() or ""
                        pdf_text += extracted_text
                    except Exception as page_e:
                        log.warning(f"Error extracting text from page {page_num}: {str(page_e)}")
            except Exception as pdf_e:
                log.error(f"Error reading PDF: {str(pdf_e)}")
    except Exception as e:
        log.error(f"Error opening file: {str(e)}")
    
    if not pdf_text or pdf_text.strip() == "":
        log.info(f"PDF contains no extractable text - might be a scanned image PDF")
    
    return pdf_text

def check_ollama_available():
    """
    Check if Ollama server is available
    
    Returns:
        tuple: (is_available, models_list, error_message)
    """
    OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://localhost:11434')
    
    for attempt in range(3):
        try:
            response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
            if response.status_code == 200:
                models = [m["name"] for m in response.json().get("models", [])]
                return True, models, None
            return False, [], f"Error status code: {response.status_code}"
        except requests.exceptions.RequestException as e:
            log.warning(f"Ollama connection attempt {attempt+1} failed: {str(e)}")
            time.sleep(1)  # Wait before retrying
    
    return False, [], "Ollama server unavailable after multiple attempts"

def extract_invoice_data(file_path, config=None):
    """Extract invoice data from a PDF file without using InvoiceScanner.
    This is a simplified version for when the scanner is not available."""
    
    # Extract text from PDF for fallback
    pdf_text = extract_text_from_pdf(file_path)
    
    # Select AI model based on file characteristics
    file_size = os.path.getsize(file_path)
    selected_model = select_ai_model(file_path, file_size)
    log.info(f"Selected AI model {selected_model} for processing {os.path.basename(file_path)} ({file_size/1024:.1f} KB)")
    
    # This is a fallback method when we can't use the full extraction
    # It's better to use InvoiceScanner from the invoice_scanner module if possible
    return {
        'success': False,
        'error': 'Direct AI extraction not implemented. Please use InvoiceScanner.',
        'raw_text': pdf_text
    }
