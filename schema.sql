-- Drop tables if they exist (for development purposes)
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS reservations;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS flights;
DROP TABLE IF EXISTS users;

-- Users table
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'agent', 'passenger')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')) -- Nueva columna para el estado del usuario
);

-- Create index for user email (used for login)
CREATE INDEX idx_users_email ON users (email);

-- Flights table
CREATE TABLE flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flight_code TEXT NOT NULL,
    date TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    capacity INTEGER NOT NULL DEFAULT 18,
    base_fare DECIMAL(10,2) NOT NULL DEFAULT 0,
    total_fare DECIMAL(10,2) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'cancelled')),
    cancellation_reason TEXT,
    change_reason TEXT,
    UNIQUE (flight_code, date)
);

-- Create index for flight date (used for filtering)
CREATE INDEX idx_flights_date ON flights (date);

-- Payments table
CREATE TABLE payments (
    id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    payment_method TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'failed', 'refunded')),
    stripe_payment_id TEXT UNIQUE,
    card_last_four TEXT,
    card_type TEXT,
    bank_name TEXT,
    error_message TEXT,
    processing_time_seconds REAL,
    transaction_date TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (reservation_id) REFERENCES reservations(id)
);

-- Customers table
CREATE TABLE customers (
    id TEXT PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    nationality TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,
    gender TEXT NOT NULL CHECK (gender IN ('M', 'F', 'Other')),
    id_number TEXT UNIQUE NOT NULL,  -- Passport or DPI
    email TEXT UNIQUE NOT NULL,
    phone TEXT NOT NULL,
    emergency_contact TEXT NOT NULL
);

-- Reservations table
CREATE TABLE reservations (
    id TEXT PRIMARY KEY,
    flight_id INTEGER NOT NULL,
    customer_id TEXT NOT NULL,
    seat_number TEXT NOT NULL,
    price REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'cancelled')),
    payment_date TEXT,
    observations TEXT,
    gate TEXT,
    boarding_time TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (flight_id) REFERENCES flights (id),
    FOREIGN KEY (customer_id) REFERENCES customers (id)
);



-- Prevent double seat assignment for active/paid reservations
CREATE UNIQUE INDEX idx_unique_active_reservation ON reservations (flight_id, seat_number)
WHERE status IN ('pending', 'paid');

-- Create index for customer's reservations
CREATE INDEX idx_reservations_customer ON reservations (customer_id);
-- Create index for flight's reservations
CREATE INDEX idx_reservations_flight ON reservations (flight_id);

-- Audit log table
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

-- Create index for audit log timestamp
CREATE INDEX idx_audit_log_timestamp ON audit_log (timestamp);





