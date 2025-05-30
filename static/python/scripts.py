# Generador de Scripts Python para UMES Air

def generate_scripts():
    """
    Genera el código JavaScript necesario para la funcionalidad del sitio UMES Air.
    """
    js_code = """
// UMES Air JavaScript

document.addEventListener('DOMContentLoaded', function() {
    // Initialize tooltips
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });

    // Flash message auto-dismiss
    setTimeout(function() {
        const alerts = document.querySelectorAll('.alert-dismissible');
        alerts.forEach(function(alert) {
            const bsAlert = new bootstrap.Alert(alert);
            bsAlert.close();
        });
    }, 5000);

    // Seat selection functionality
    setupSeatSelection();
    
    // Flight search form validation
    setupFlightSearch();
    
    // Confirm actions (delete, cancel)
    setupConfirmActions();
    
    // Date input constraints and validation
    setupDateInputs();
});

function setupSeatSelection() {
    const seatMap = document.querySelector('.seat-map');
    const selectedSeatInput = document.getElementById('selected-seat');
    const seatNumberInput = document.getElementById('seat_number');
    
    if (seatMap) {
        seatMap.addEventListener('click', function(e) {
            if (e.target.classList.contains('seat') && !e.target.classList.contains('seat-occupied')) {
                // Deselect any previously selected seat
                document.querySelectorAll('.seat-selected').forEach(function(seat) {
                    seat.classList.remove('seat-selected');
                });
                
                // Select this seat
                e.target.classList.add('seat-selected');
                
                // Update the hidden input value
                if (selectedSeatInput) {
                    selectedSeatInput.textContent = e.target.textContent;
                }
                
                // Update the form input if present
                if (seatNumberInput) {
                    seatNumberInput.value = e.target.textContent;
                }
                
                // Enable the reserve button if it exists
                const reserveButton = document.getElementById('reserve-button');
                if (reserveButton) {
                    reserveButton.disabled = false;
                }
            }
        });
    }
}

function setupFlightSearch() {
    const flightSearchForm = document.getElementById('flight-search-form');
    
    if (flightSearchForm) {
        flightSearchForm.addEventListener('submit', function(e) {
            const originInput = document.getElementById('origin');
            const destinationInput = document.getElementById('destination');
            const dateInput = document.getElementById('date');
            
            let isValid = true;
            
            // Simple validation
            if (originInput && !originInput.value) {
                isValid = false;
                showValidationError(originInput, 'Please select an origin');
            }
            
            if (destinationInput && !destinationInput.value) {
                isValid = false;
                showValidationError(destinationInput, 'Please select a destination');
            }
            
            if (dateInput && !dateInput.value) {
                isValid = false;
                showValidationError(dateInput, 'Please select a date');
            }
            
            if (!isValid) {
                e.preventDefault();
            }
        });
    }
}

function showValidationError(inputElement, message) {
    inputElement.classList.add('is-invalid');
    
    // Create or update error message
    let errorDiv = inputElement.nextElementSibling;
    if (!errorDiv || !errorDiv.classList.contains('invalid-feedback')) {
        errorDiv = document.createElement('div');
        errorDiv.className = 'invalid-feedback';
        inputElement.parentNode.insertBefore(errorDiv, inputElement.nextSibling);
    }
    
    errorDiv.textContent = message;
    
    // Remove error on input change
    inputElement.addEventListener('input', function() {
        this.classList.remove('is-invalid');
    }, { once: true });
}

function setupConfirmActions() {
    const confirmButtons = document.querySelectorAll('[data-confirm]');
    
    confirmButtons.forEach(function(button) {
        button.addEventListener('click', function(e) {
            if (!confirm(this.dataset.confirm)) {
                e.preventDefault();
            }
        });
    });
}

function setupDateInputs() {
    const dateInputs = document.querySelectorAll('input[type="date"]');
    
    dateInputs.forEach(function(input) {
        // Set min date to today for future date selections
        if (input.dataset.futureOnly) {
            const today = new Date().toISOString().split('T')[0];
            input.min = today;
        }
    });
}

// Flight seat map functionality
function changeSeat(flightId, reservationId) {
    const selectedSeat = document.querySelector('.seat-selected');
    const seatInput = document.getElementById('new-seat');
    const changeButton = document.querySelector('#change-seat-form button');
    
    if (!selectedSeat) {
        alert('Por favor seleccione un asiento primero');
        return false;
    }
    
    seatInput.value = selectedSeat.textContent.trim();
    changeButton.disabled = false;
    
    document.getElementById('change-seat-form').submit();
    return true;
}

// Print boarding pass
function printBoardingPass() {
    window.print();
}

// Flight status updates
function showFlightStatus(flightId, status) {
    const statusElement = document.getElementById(`flight-status-${flightId}`);
    
    if (statusElement) {
        statusElement.textContent = status;
        
        // Update colors based on status
        statusElement.className = 'badge';
        
        switch(status.toLowerCase()) {
            case 'on time':
                statusElement.classList.add('badge-success');
                break;
            case 'delayed':
                statusElement.classList.add('badge-warning');
                break;
            case 'cancelled':
                statusElement.classList.add('badge-danger');
                break;
            default:
                statusElement.classList.add('badge-primary');
        }
    }
}

// Filter flight tables
function filterTable(input, tableId) {
    const filter = input.value.toUpperCase();
    const table = document.getElementById(tableId);
    
    if (!table) return;
    
    const rows = table.querySelectorAll('tbody tr');
    
    rows.forEach(function(row) {
        const cells = row.querySelectorAll('td');
        let found = false;
        
        cells.forEach(function(cell) {
            if (cell.textContent.toUpperCase().indexOf(filter) > -1) {
                found = true;
            }
        });
        
        row.style.display = found ? '' : 'none';
    });
}
    """
    return js_code

# Función para servir el JavaScript desde Flask
def get_js():
    """
    Retorna el código JavaScript generado para ser servido por Flask.
    """
    return generate_scripts()