/**
 * Main JavaScript file for the Invoice Management System
 */

// Set current year in footer
document.addEventListener('DOMContentLoaded', function() {
    // Update year in footer
    const footerYearElements = document.querySelectorAll('.footer');
    const currentYear = new Date().getFullYear();
    
    footerYearElements.forEach(element => {
        if (element.textContent.includes('{{ now.year }}')) {
            element.innerHTML = element.innerHTML.replace('{{ now.year }}', currentYear);
        }
    });
    
    // Add fade-in class to main content
    const mainContent = document.querySelector('.container-fluid');
    if (mainContent) {
        mainContent.classList.add('fade-in');
    }
    
    // Initialize popovers and tooltips if Bootstrap is available
    if (typeof bootstrap !== 'undefined') {
        // Initialize popovers
        const popoverTriggerList = document.querySelectorAll('[data-bs-toggle="popover"]');
        const popoverList = [...popoverTriggerList].map(popoverTriggerEl => new bootstrap.Popover(popoverTriggerEl));
        
        // Initialize tooltips
        const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
        const tooltipList = [...tooltipTriggerList].map(tooltipTriggerEl => new bootstrap.Tooltip(tooltipTriggerEl));
    }
    
    // Add active class to appropriate nav link
    const currentPath = window.location.pathname;
    const navLinks = document.querySelectorAll('.navbar-nav .nav-link');
    
    navLinks.forEach(link => {
        if (link.getAttribute('href') === currentPath) {
            link.classList.add('active');
        } else {
            link.classList.remove('active');
        }
    });
});

/**
 * Format a number as currency
 * @param {number} amount - The amount to format
 * @param {string} locale - The locale to use (default: 'de-DE')
 * @param {string} currency - The currency to use (default: 'EUR')
 * @returns {string} Formatted currency string
 */
function formatCurrency(amount_original, locale = 'de-DE', currency = 'EUR') {
    if (amount_original === null || amount_original === undefined || amount_original === '') {
        return '';
    }
    // Remove currency symbols and spaces, replace comma with dot
    let cleaned = String(amount_original).replace(/[^\d,.-]/g, '').replace(',', '.');
    let num = parseFloat(cleaned);
    if (isNaN(num)) {
        return amount_original; // fallback to raw string if not a number
    }
    return new Intl.NumberFormat(locale, { 
        style: 'currency', 
        currency: currency 
    }).format(num);
}

/**
 * Format a date string
 * @param {string} dateStr - The date string to format
 * @param {string} locale - The locale to use (default: 'de-DE')
 * @returns {string} Formatted date string
 */
function formatDate(dateStr, locale = 'de-DE') {
    if (!dateStr) return 'N/A';
    
    try {
        const date = new Date(dateStr);
        return new Intl.DateTimeFormat(locale, { 
            year: 'numeric', 
            month: 'long', 
            day: 'numeric' 
        }).format(date);
    } catch (e) {
        console.error('Error formatting date:', e);
        return dateStr;
    }
}

/**
 * Show a toast message
 * @param {string} message - The message to display
 * @param {string} type - The type of toast (success, error, warning, info)
 * @param {number} duration - Duration in milliseconds
 */
function showToast(message, type = 'info', duration = 3000) {
    // Create toast container if it doesn't exist
    let toastContainer = document.getElementById('toast-container');
    
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'toast-container';
        toastContainer.className = 'position-fixed bottom-0 end-0 p-3';
        toastContainer.style.zIndex = '1050';
        document.body.appendChild(toastContainer);
    }
    
    // Map type to Bootstrap class and icon
    const typeMap = {
        success: {
            bgClass: 'bg-success',
            icon: 'bi-check-circle'
        },
        error: {
            bgClass: 'bg-danger',
            icon: 'bi-exclamation-circle'
        },
        warning: {
            bgClass: 'bg-warning',
            icon: 'bi-exclamation-triangle'
        },
        info: {
            bgClass: 'bg-info',
            icon: 'bi-info-circle'
        }
    };
    
    const toastType = typeMap[type] || typeMap.info;
    
    // Create the toast
    const toastId = `toast-${Date.now()}`;
    const toast = document.createElement('div');
    toast.id = toastId;
    toast.className = `toast ${toastType.bgClass} text-white`;
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'assertive');
    toast.setAttribute('aria-atomic', 'true');
    
    // Toast content
    toast.innerHTML = `
        <div class="toast-header ${toastType.bgClass} text-white">
            <i class="bi ${toastType.icon} me-2"></i>
            <strong class="me-auto">Invoice Manager</strong>
            <button type="button" class="btn-close btn-close-white" data-bs-dismiss="toast" aria-label="Close"></button>
        </div>
        <div class="toast-body">
            ${message}
        </div>
    `;
    
    // Add the toast to the container
    toastContainer.appendChild(toast);
    
    // Show the toast
    const bsToast = new bootstrap.Toast(toast, {
        autohide: true,
        delay: duration
    });
    
    bsToast.show();
    
    // Remove the toast element after it's hidden
    toast.addEventListener('hidden.bs.toast', () => {
        toast.remove();
    });
}

/**
 * Handle API errors
 * @param {Error} error - The error object
 * @param {string} defaultMessage - Default message to show if error details are not available
 */
function handleApiError(error, defaultMessage = 'An error occurred.') {
    let errorMessage = defaultMessage;
    
    if (error.response) {
        // The request was made and the server responded with a status code
        // that falls out of the range of 2xx
        errorMessage = error.response.data.error || `Error ${error.response.status}: ${error.response.statusText}`;
    } else if (error.request) {
        // The request was made but no response was received
        errorMessage = 'No response received from server. Please check your connection.';
    } else {
        // Something happened in setting up the request that triggered an Error
        errorMessage = error.message || defaultMessage;
    }
    
    // Show error in toast
    showToast(errorMessage, 'error');
    
    // Log the full error for debugging
    console.error('API Error:', error);
}

/**
 * Debounce function to limit how often a function can be called
 * @param {Function} func - The function to debounce
 * @param {number} wait - The debounce time in milliseconds
 * @returns {Function} Debounced function
 */
function debounce(func, wait = 300) {
    let timeout;
    
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Cache management functions for client-side caching
 */
const cacheManager = {
    /**
     * Save data to localStorage with an expiration time
     * @param {string} key - The cache key
     * @param {any} data - The data to cache
     * @param {number} expirationMinutes - Cache expiration time in minutes (default: 15)
     */
    saveToCache: function(key, data, expirationMinutes = 15) {
        try {
            const expirationMs = expirationMinutes * 60 * 1000;
            const item = {
                data: data,
                expiration: Date.now() + expirationMs
            };
            localStorage.setItem(key, JSON.stringify(item));
            console.log(`Cached data for key: ${key}`);
        } catch (error) {
            console.error('Error saving to cache:', error);
        }
    },
    
    /**
     * Get data from localStorage cache if not expired
     * @param {string} key - The cache key
     * @returns {any|null} - The cached data or null if expired/not found
     */
    getFromCache: function(key) {
        try {
            const item = localStorage.getItem(key);
            if (!item) return null;
            
            const cachedItem = JSON.parse(item);
            const now = Date.now();
            
            if (now > cachedItem.expiration) {
                // Cache expired, remove it
                localStorage.removeItem(key);
                console.log(`Cache expired for key: ${key}`);
                return null;
            }
            
            console.log(`Retrieved data from cache for key: ${key}`);
            return cachedItem.data;
        } catch (error) {
            console.error('Error getting from cache:', error);
            return null;
        }
    },
    
    /**
     * Clear a specific cache item
     * @param {string} key - The cache key to clear
     */
    clearCacheItem: function(key) {
        try {
            localStorage.removeItem(key);
            console.log(`Cleared cache for key: ${key}`);
        } catch (error) {
            console.error('Error clearing cache item:', error);
        }
    },
    
    /**
     * Clear all cache items related to invoices
     */
    clearInvoiceCache: function() {
        try {
            const keysToRemove = [];
            
            // Find all invoice-related cache items
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                if (key && (key.startsWith('invoice-') || key.startsWith('folder-') || key.startsWith('supplier-'))) {
                    keysToRemove.push(key);
                }
            }
            
            // Remove them
            keysToRemove.forEach(key => localStorage.removeItem(key));
            console.log(`Cleared ${keysToRemove.length} invoice cache items`);
        } catch (error) {
            console.error('Error clearing invoice cache:', error);
        }
    },
    
    /**
     * Enhanced fetch that uses cache
     * @param {string} url - The URL to fetch
     * @param {string} cacheKey - The cache key to use
     * @param {number} expirationMinutes - Cache expiration time in minutes
     * @param {boolean} forceRefresh - Whether to force a refresh bypassing cache
     * @returns {Promise<any>} - The fetched or cached data
     */
    fetchWithCache: async function(url, cacheKey, expirationMinutes = 15, forceRefresh = false) {
        if (!forceRefresh) {
            // Try to get from cache first
            const cachedData = this.getFromCache(cacheKey);
            if (cachedData) {
                return cachedData;
            }
        }
        
        try {
            // If not in cache or force refresh, fetch from server
            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            
            const data = await response.json();
            
            // Save to cache for future use
            this.saveToCache(cacheKey, data, expirationMinutes);
            
            return data;
        } catch (error) {
            console.error('Error fetching data:', error);
            throw error;
        }
    }
}; 