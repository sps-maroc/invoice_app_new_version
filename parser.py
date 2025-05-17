import logging
import re
from datetime import datetime
from pydantic import BaseModel, Field, validator, root_validator
from typing import Optional, Dict, Any, Union

log = logging.getLogger(__name__)

class InvoiceFields(BaseModel):
    """Pydantic model for German invoice fields"""
    Lieferantename: str = Field(description="Name of the supplier (the company issuing the invoice)")
    Rechnungsdatum: str = Field(description="Invoice date in format DD.MM.YYYY or YYYY-MM-DD")
    Gesamtbetrag: str = Field(description="Total amount with currency (e.g. '120,50 EUR')")
    Empfängerfirma: str = Field(description="Name of the recipient company")
    Rechnungsnummer: str = Field(description="Invoice number/identifier")
    Mehrwertsteuerbetrag: str = Field(description="VAT amount with currency")
    Leistungsbeschreibung: str = Field(description="Description of services or goods")
    
    class Config:
        """Config for the Pydantic model"""
        schema_extra = {
            "example": {
                "Lieferantename": "Tech Solutions GmbH",
                "Rechnungsdatum": "15.04.2023",
                "Gesamtbetrag": "1.234,56 EUR",
                "Empfängerfirma": "Kunde AG",
                "Rechnungsnummer": "RE-2023-12345",
                "Mehrwertsteuerbetrag": "234,56 EUR",
                "Leistungsbeschreibung": "IT Dienstleistungen und Hardware"
            }
        }
        
    @validator("Rechnungsdatum")
    def validate_date_format(cls, v):
        """Validate and normalize invoice date format"""
        if not v:
            return v
            
        # Try to parse common German/European date formats
        date_formats = [
            "%d.%m.%Y",    # 31.12.2023
            "%Y-%m-%d",    # 2023-12-31
            "%d/%m/%Y",    # 31/12/2023
            "%d-%m-%Y",    # 31-12-2023
            "%d.%m.%y",    # 31.12.23
            "%Y/%m/%d"     # 2023/12/31
        ]
        
        original_value = v
        # Remove any extra whitespace
        v = v.strip()
        
        for date_format in date_formats:
            try:
                parsed_date = datetime.strptime(v, date_format)
                # Return standardized format
                return parsed_date.strftime("%d.%m.%Y")
            except ValueError:
                continue
                
        # If we couldn't parse it with known formats, return original but log a warning
        log.warning(f"Could not normalize date format: {original_value}")
        return original_value
        
    @validator("Gesamtbetrag", "Mehrwertsteuerbetrag")
    def validate_amount_format(cls, v):
        """Validate and normalize amount format"""
        if not v:
            return v
            
        # Try to extract the number and currency
        amount_match = re.search(r'([0-9.,]+)\s*([A-Za-z€£$]*)', v.strip())
        if amount_match:
            amount_str = amount_match.group(1)
            currency = amount_match.group(2) or "EUR"
            
            # Normalize number format (German style with comma as decimal separator)
            # First, handle German format (1.234,56)
            if '.' in amount_str and ',' in amount_str:
                amount_str = amount_str.replace('.', '')
                amount_str = amount_str.replace(',', '.')
            # Handle single comma as decimal separator (1234,56)
            elif ',' in amount_str and '.' not in amount_str:
                amount_str = amount_str.replace(',', '.')
                
            # Try to parse as float to validate
            try:
                amount = float(amount_str)
                # Format with German style (comma as decimal separator)
                formatted_amount = f"{amount:.2f}".replace('.', ',')
                return f"{formatted_amount} {currency.strip()}"
            except ValueError:
                pass
                
        # Return original if we couldn't parse it
        return v


class EnhancedInvoiceFields(BaseModel):
    """Enhanced Pydantic model for invoice fields with both German and English field names"""
    # German fields
    Lieferantename: str = Field("", description="Name of the supplier (German)")
    Rechnungsdatum: str = Field("", description="Invoice date (German)")
    Gesamtbetrag: str = Field("", description="Total amount (German)")
    Empfängerfirma: str = Field("", description="Recipient company (German)")
    Rechnungsnummer: str = Field("", description="Invoice number (German)")
    Mehrwertsteuerbetrag: str = Field("", description="VAT amount (German)")
    Leistungsbeschreibung: str = Field("", description="Service description (German)")
    
    # English fields
    supplier_name: str = Field("", description="Name of the supplier (English)")
    invoice_date: str = Field("", description="Invoice date (English)")
    amount: str = Field("", description="Total amount (English)")
    company_name: str = Field("", description="Recipient company (English)")
    invoice_number: str = Field("", description="Invoice number (English)")
    vat_amount: str = Field("", description="VAT amount (English)")
    description: str = Field("", description="Service description (English)")
    
    # Additional useful fields
    due_date: Optional[str] = Field("", description="Due date for payment")
    currency: Optional[str] = Field("EUR", description="Currency")
    vat_rate: Optional[str] = Field("", description="VAT rate percentage")
    subtotal: Optional[str] = Field("", description="Subtotal amount before VAT")
    success: bool = Field(True, description="Whether extraction was successful")
    
    @root_validator(pre=True)
    def sync_german_english_fields(cls, values):
        """Sync German and English field names to ensure both are populated"""
        field_map = {
            "Lieferantename": "supplier_name",
            "Rechnungsdatum": "invoice_date",
            "Gesamtbetrag": "amount", 
            "Empfängerfirma": "company_name",
            "Rechnungsnummer": "invoice_number",
            "Mehrwertsteuerbetrag": "vat_amount",
            "Leistungsbeschreibung": "description"
        }
        
        # Copy from German to English fields if English fields are empty
        for german, english in field_map.items():
            if german in values and values.get(german) and english in values and not values.get(english):
                values[english] = values[german]
        
        # Copy from English to German fields if German fields are empty
        for german, english in field_map.items():
            if english in values and values.get(english) and german in values and not values.get(german):
                values[german] = values[english]
                
        return values
        
    @validator("invoice_date", "Rechnungsdatum", "due_date")
    def validate_date_format(cls, v):
        """Validate and normalize date format"""
        if not v:
            return v
            
        # Try to parse common date formats
        date_formats = [
            "%d.%m.%Y",    # 31.12.2023
            "%Y-%m-%d",    # 2023-12-31
            "%d/%m/%Y",    # 31/12/2023
            "%d-%m-%Y",    # 31-12-2023
            "%d.%m.%y",    # 31.12.23
            "%Y/%m/%d"     # 2023/12/31
        ]
        
        v = v.strip()
        
        for date_format in date_formats:
            try:
                parsed_date = datetime.strptime(v, date_format)
                # Return standardized format
                return parsed_date.strftime("%d.%m.%Y")
            except ValueError:
                continue
                
        # Return original if we couldn't parse it
        return v
        
    class Config:
        """Config for the Pydantic model"""
        extra = "allow"  # Allow extra fields
        
def validate_invoice_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and normalize invoice data using the EnhancedInvoiceFields model
    
    Args:
        data: Dictionary containing invoice data
        
    Returns:
        Dict: Validated and normalized invoice data
    """
    try:
        # First try with the enhanced model
        validated_data = EnhancedInvoiceFields(**data).dict()
        return {**validated_data, "validation_success": True}
    except Exception as e:
        log.warning(f"Enhanced validation failed: {str(e)}")
        try:
            # Try with the simpler model
            validated_data = InvoiceFields(**data).dict()
            return {**validated_data, "validation_success": True}
        except Exception as e2:
            log.error(f"Basic validation also failed: {str(e2)}")
            # Return original data with validation failure flag
            return {**data, "validation_success": False, "validation_error": str(e2)}
            
def normalize_amount(amount_str: str) -> float:
    """
    Convert various amount string formats to a float value
    
    Args:
        amount_str: String representing an amount (e.g., "1.234,56 EUR")
        
    Returns:
        float: Normalized amount as a float
    """
    if not amount_str:
        return 0.0
    
    # If already a number, just return it
    if isinstance(amount_str, (int, float)):
        return float(amount_str)
    
    # Try to extract just the numeric part
    amount_str = str(amount_str).strip()
    
    # Remove currency symbols and whitespace
    amount_str = re.sub(r'[€$£\s]', '', amount_str)
    
    # Handle German number format (1.234,56)
    if '.' in amount_str and ',' in amount_str:
        amount_str = amount_str.replace('.', '')
        amount_str = amount_str.replace(',', '.')
    # Handle single comma as decimal separator (1234,56)
    elif ',' in amount_str and '.' not in amount_str:
        amount_str = amount_str.replace(',', '.')
    
    try:
        return float(amount_str)
    except ValueError:
        log.warning(f"Could not parse amount: {amount_str}")
        return 0.0 