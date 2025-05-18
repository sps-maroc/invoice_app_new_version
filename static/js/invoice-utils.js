/**
 * Invoice Processing Application - Utility Functions
 * A collection of reusable functions for handling invoice data
 */

/**
 * Extract normalized field values from an invoice data object
 * Handles various nested data structures and field name variations
 * 
 * @param {Object} data - The raw data object containing invoice information
 * @param {Object} fieldMap - Mapping of target fields to possible source field names
 * @returns {Object} - Object with normalized field values
 */
function extractInvoiceData(data, fieldMap = {}) {
    // Default field mapping if not provided
    const mapping = fieldMap || {
        'supplier_name': ['supplier_name', 'Lieferantename'],
        'company_name': ['company_name', 'Empfängerfirma'],
        'invoice_number': ['invoice_number', 'Rechnungsnummer'],
        'invoice_date': ['invoice_date', 'Rechnungsdatum'],
        'amount_original': ['amount_original', 'amount', 'Gesamtbetrag'],
        'vat_amount_original': ['vat_amount_original', 'vat_amount', 'Mehrwertsteuerbetrag'],
        'description': ['description', 'Leistungsbeschreibung'],
        'file_path': ['file_path', 'original_path']
    };
    
    // Result object with extracted fields
    const result = {};
    
    // Check if data exists
    if (!data) {
        console.error("No data provided to extractInvoiceData");
        return result;
    }
    
    // Try to find values directly in the data object
    for (const [targetField, sourceFields] of Object.entries(mapping)) {
        for (const sourceField of sourceFields) {
            if (data[sourceField] !== undefined) {
                result[targetField] = data[sourceField];
                break;
            }
        }
    }
    
    // Look for nested data structures
    const possibleNestedPaths = ['invoice_data', 'data', 'extracted_data'];
    
    for (const path of possibleNestedPaths) {
        if (data[path]) {
            // If it's an object, try to extract values
            if (typeof data[path] === 'object') {
                for (const [targetField, sourceFields] of Object.entries(mapping)) {
                    if (result[targetField] !== undefined) continue; // Skip if already found
                    
                    for (const sourceField of sourceFields) {
                        if (data[path][sourceField] !== undefined) {
                            result[targetField] = data[path][sourceField];
                            break;
                        }
                    }
                }
            } 
            // If it's a JSON string, try to parse it
            else if (typeof data[path] === 'string') {
                try {
                    const parsedData = JSON.parse(data[path]);
                    for (const [targetField, sourceFields] of Object.entries(mapping)) {
                        if (result[targetField] !== undefined) continue; // Skip if already found
                        
                        for (const sourceField of sourceFields) {
                            if (parsedData[sourceField] !== undefined) {
                                result[targetField] = parsedData[sourceField];
                                break;
                            }
                        }
                    }
                } catch (e) {
                    console.warn(`Failed to parse JSON string in field ${path}:`, e);
                }
            }
        }
    }
    
    return result;
}

/**
 * Fill a form with invoice data
 * 
 * @param {HTMLFormElement} form - The form element to fill
 * @param {Object} data - Invoice data object
 * @param {Object} fieldMap - Mapping of form field IDs to data field names
 */
function fillInvoiceForm(form, data, fieldMap = {}) {
    // Default field mapping if not provided
    const mapping = fieldMap || {
        'supplier-name': 'supplier_name',
        'company-name': 'company_name',
        'invoice-number': 'invoice_number',
        'invoice-date': 'invoice_date',
        'amount-original': 'amount_original',
        'vat-amount-original': 'vat_amount_original',
        'description': 'description',
        'file-path': 'file_path'
    };
    
    // Extract normalized data
    const normalized = extractInvoiceData(data);
    
    // Fill form fields
    for (const [elementId, dataField] of Object.entries(mapping)) {
        const element = form.querySelector(`#${elementId}`) || document.getElementById(elementId);
        if (element && normalized[dataField] !== undefined) {
            element.value = normalized[dataField];
        }
    }
    
    return normalized;
}

/**
 * Format and validate invoice fields
 * 
 * @param {Object} data - Object containing invoice data to validate
 * @returns {Object} - Validated and formatted data
 */
function validateInvoiceData(data) {
    const result = { ...data };
    // Only format the invoice date if needed, do not touch amount fields
    if (result.invoice_date) {
        try {
            const date = new Date(result.invoice_date);
            if (!isNaN(date.getTime())) {
                result.invoice_date = date.toISOString().split('T')[0];
            }
        } catch (e) {
            console.warn("Could not parse date:", result.invoice_date);
        }
    }
    // Do NOT clean or parse amount_original or vat_amount_original
    return result;
}

/**
 * Format amount for display
 * 
 * @param {number|string} amount - Amount to format
 * @param {string} locale - Locale to use for formatting
 * @param {string} currency - Currency code
 * @returns {string} - Formatted amount string
 */
function formatInvoiceAmount(amount, locale = 'de-DE', currency = 'EUR') {
    if (amount === null || amount === undefined || isNaN(parseFloat(amount))) {
        return 'N/A';
    }
    
    const numAmount = typeof amount === 'string' ? 
        parseFloat(amount.replace(/[^\d.,]/g, '').replace(/,/g, '.')) : 
        parseFloat(amount);
    
    return new Intl.NumberFormat(locale, { 
        style: 'currency', 
        currency: currency 
    }).format(numAmount);
}

/**
 * Extract text filename from a file path
 * 
 * @param {string} path - File path
 * @returns {string} - Filename
 */
function extractFilename(path) {
    if (!path) return 'Unknown';
    
    // Handle both Windows and Unix paths
    const parts = path.split(/[/\\]/);
    return parts[parts.length - 1];
}

/**
 * Check if an API response indicates success
 * 
 * @param {Object} response - API response object
 * @returns {boolean} - True if the response indicates success
 */
function isSuccessResponse(response) {
    return response && response.success === true;
} 