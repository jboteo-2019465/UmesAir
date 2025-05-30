import os
import sqlite3
import hashlib
import uuid
import random
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file, Response, make_response
from weasyprint import HTML, CSS # Added for PDF generation
import qrcode # For QR code generation
import io # For handling byte streams
import base64 # For encoding QR code image
import random # For generating random gate and boarding time
from datetime import datetime, timedelta # For flight duration and boarding time calculation
from static.python.scripts import get_js
import stripe
from dotenv import load_dotenv
from flask_mail import Mail, Message
from flask_apscheduler import APScheduler

# Custom adaptador para datetime a formato ISO
def adapt_datetime_iso(val):
    """Adaptador para datetime a formato ISO 8601."""
    return val.isoformat()

def convert_datetime_iso(val):
    """Convertir de ISO 8601 a datetime."""
    return datetime.fromisoformat(val.decode())

# Register the adapter and converter
sqlite3.register_adapter(datetime, adapt_datetime_iso)
sqlite3.register_converter("DATETIME", convert_datetime_iso)
sqlite3.register_converter("TIMESTAMP", convert_datetime_iso) # Also handle TIMESTAMP type if used

# Cargar variables de entorno
load_dotenv()

# Configurar Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
stripe_keys = {
    "secret_key": os.getenv('STRIPE_SECRET_KEY'),
    "publishable_key": os.getenv('STRIPE_PUBLISHABLE_KEY'),
    "endpoint_secret": os.getenv('STRIPE_ENDPOINT_SECRET')
}

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['DATABASE'] = 'umes_air.db'

# Configuración Flask-Mail (El usuario DEBE reemplazar estos valores)
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'false').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME', 'juancarlosboteogranados@gmail.com')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD', 'zark orbw mfvf czpp')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', 'Umes Air <juancarlosboteogranados@gmail.com>') # Puede ser solo el email o 'Nombre <email@example.com>'

mail = Mail(app)

# Configuración del Programador
scheduler = APScheduler()

# Variable para asegurar que el scheduler solo se inicie una vez en el proceso principal de Flask
scheduler_started = False

# Helper para generar CSRF token (si no existe uno más robusto)
import secrets
def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']

@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf_token)

# Agregar filtro personalizado para formatear fechas
@app.template_filter('strftime')
def _jinja2_filter_strftime(date_format, timestamp=None):
    if timestamp is None or timestamp == 'None':
        timestamp = datetime.now()
    elif isinstance(timestamp, str) and not timestamp == 'None':
        try:
            timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            timestamp = datetime.now()
    return timestamp.strftime(date_format)

# Bandera para rastrear si la configuración se ha ejecutado
_is_first_request = True

# ------- Database Setup -------
def get_db_connection():
    conn = sqlite3.connect(app.config['DATABASE'], timeout=30)  # Increased timeout to 30 seconds
    print("Connected to database")
    conn.row_factory = sqlite3.Row
    return conn
 
def init_db():
    """Inicializa la base de datos solo si no existe o está vacía"""

# ------- Email Sending Function -------
def send_email(subject, sender_email=None, recipients=None, html_body=None, to=None, template=None, **kwargs):
    """Función para enviar correos electrónicos usando plantillas HTML o HTML directo."""
    try:
        # Compatibilidad con ambos formatos de llamada
        if to and not recipients:
            recipients = [to]
        if not sender_email:
            sender_email = app.config['MAIL_DEFAULT_SENDER']
        
        # Asegurarse de que el remitente esté bien formado
        if isinstance(sender_email, tuple):
            sender = sender_email
        elif '<' in sender_email and '>' in sender_email:
            sender = sender_email
        else:
            sender = f"UMES AIR <{sender_email}>"

        msg = Message(
            subject,
            sender=sender,
            recipients=recipients
        )
        
        # Si se proporciona html_body directamente, usarlo
        if html_body:
            msg.html = html_body
        # Si se proporciona template, renderizarlo
        elif template:
            kwargs['team_name'] = 'El equipo de UMES AIR'
            msg.html = render_template(f'emails/{template}', **kwargs)
        else:
            raise ValueError("Debe proporcionar html_body o template")
            
        mail.send(msg)
        recipient_list = ', '.join(recipients) if isinstance(recipients, list) else recipients
        log_action(f"Correo enviado a {recipient_list} con asunto: {subject}", "info")
        return True
    except Exception as e:
        recipient_list = ', '.join(recipients) if isinstance(recipients, list) else recipients
        app.logger.error(f"Error al enviar correo a {recipient_list} con asunto {subject}: {str(e)}")
        log_action(f"Error al enviar correo a {recipient_list}: {str(e)}", "error")
        return False

# @app.route('/send_flight_reminders') # Se podría proteger esta ruta o llamarla desde un cron job
def send_flight_reminders_job():
    # Es importante crear un contexto de aplicación aquí si la tarea se ejecuta fuera del contexto de una solicitud
    with app.app_context():
        conn = get_db_connection()
        now = datetime.now(timezone.utc) # Usar timezone-aware datetime
        # Convertir la hora de la base de datos (asumida como local/naive) a UTC para comparación si es necesario
        # Ajustamos la ventana para que sea más precisa para "30 minutos antes"
        # El job corre cada minuto, así que buscamos vuelos en la ventana de los próximos 29 a 30 minutos.
        reminder_target_time_start = now + timedelta(minutes=29)
        reminder_target_time_end = now + timedelta(minutes=30)


        # Primero, obtenemos todos los vuelos activos para el día de hoy y mañana (para cubrir vuelos de medianoche)
        today_str = now.strftime('%Y-%m-%d')
        tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')

        upcoming_flights_today_or_tomorrow = conn.execute('''
            SELECT id, flight_code, date, origin, destination, departure_time, status
            FROM flights
            WHERE status = 'active' AND (date = ? OR date = ?)
        ''', (today_str, tomorrow_str)).fetchall()

        reminders_sent_count = 0
        errors_count = 0

        for flight_row in upcoming_flights_today_or_tomorrow:
            try:
                # Convertir la fecha y hora del vuelo a un objeto datetime timezone-aware (UTC)
                flight_departure_dt_naive = datetime.strptime(f"{flight_row['date']} {flight_row['departure_time']}", '%Y-%m-%d %H:%M:%S')

                flight_departure_dt_utc = flight_departure_dt_naive.replace(tzinfo=timezone.utc) # ASUME QUE LA BD GUARDA UTC

                # Verificar si la hora de salida del vuelo está dentro de nuestra ventana de recordatorio
                if not (reminder_target_time_start <= flight_departure_dt_utc < reminder_target_time_end):
                    continue # Saltar este vuelo si no está en la ventana de 30 minutos

                # Verificar si ya se envió un recordatorio para este vuelo (para evitar duplicados)

                reservations = conn.execute('''
                    SELECT r.id as reservation_id, r.seat_number, c.email, c.first_name, c.last_name
                    FROM reservations r
                    JOIN customers c ON r.customer_id = c.id
                    WHERE r.flight_id
                ''', (flight_row['id'],)).fetchall()

                for res in reservations:
                    try:
                        # Renderizar la plantilla de correo electrónico
                        send_email(subject=f"Recordatorio: Tu Vuelo {flight_row['flight_code']} está Próximo a Partir - UMES AIR",
                                   recipients=[res['email']],
                                   template='flight_reminder_email.html',
                                   customer_name=f"{res['first_name']} {res['last_name']}",
                                   reservation=res,
                                   flight=flight_row,
                                   now=datetime.now(timezone.utc))
                        log_action(f"Correo de recordatorio de vuelo {flight_row['flight_code']} enviado a {res['email']} para reserva ID {res['reservation_id']}", "success")
                        reminders_sent_count += 1
                    except Exception as e:
                        log_action(f"Error enviando correo de recordatorio de vuelo {flight_row['flight_code']} a {res['email']} (reserva ID {res['reservation_id']}): {str(e)}", "error")
                        app.logger.error(f"Error enviando correo de recordatorio de vuelo {flight_row['flight_code']} a {res['email']} (reserva ID {res['reservation_id']}): {str(e)}")
                        errors_count += 1
            except Exception as e:
                log_action(f"Error procesando recordatorios para vuelo ID {flight_row['id']}: {str(e)}", "error")
                app.logger.error(f"Error procesando recordatorios para vuelo ID {flight_row['id']}: {str(e)}")
                errors_count +=1
        
        conn.close()
        if reminders_sent_count > 0 or errors_count > 0:
            log_action(f'Tarea de recordatorios: {reminders_sent_count} enviados, {errors_count} errores.', 'info')
        # return redirect(url_for('dashboard')) # O alguna página de admin

# ------- Rutas de la aplicación ------- 

@app.before_request
def ensure_db_initialized():
    global _is_first_request
    if _is_first_request:
        # init_db() # Asegúrate que init_db() es seguro llamarlo múltiples veces o solo una vez
        _is_first_request = False

# Inicializar y empezar el scheduler solo si no estamos en un subproceso de Werkzeug (reloader)
# y si no ha sido iniciado ya.
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
    if not scheduler.running:
        scheduler.init_app(app)
        scheduler.add_job(id='send_flight_reminders_task', func=send_flight_reminders_job, trigger='interval', minutes=1)
        scheduler.start()
        print("Scheduler started for flight reminders.")
        #log_action("Scheduler started for flight reminders.", "info")

@app.route('/')
def index():
    # Ejemplo: Obtener algunos vuelos para mostrar en la página de inicio
    conn = get_db_connection()
    # Obtener solo vuelos activos y futuros
    today = datetime.now().strftime('%Y-%m-%d')
    flights = conn.execute('''
        SELECT * FROM flights 
        WHERE status = 'active' AND date >= ? 
        ORDER BY date, departure_time LIMIT 6
    ''', (today,)).fetchall()
    conn.close()
    return render_template('index.html', flights=flights)


# ------- Dashboard -------
    conn = get_db_connection()
    now = datetime.now()
    reminder_window_start = now + timedelta(minutes=25) # Un poco antes para dar margen
    reminder_window_end = now + timedelta(minutes=35)   # Un poco después para dar margen

    # Buscar vuelos activos programados para despegar en la ventana de tiempo
    upcoming_flights = conn.execute('''
        SELECT id, flight_code, date, origin, destination, departure_time
        FROM flights
        WHERE status = 'active' AND date = ? 
        AND time(departure_time) >= time(?) AND time(departure_time) <= time(?)
    ''', (now.strftime('%Y-%m-%d'), reminder_window_start.strftime('%H:%M:%S'), reminder_window_end.strftime('%H:%M:%S'))).fetchall()

    reminders_sent_count = 0
    errors_count = 0

    for flight in upcoming_flights:
        # Verificar si ya se envió un recordatorio para este vuelo recientemente (evitar duplicados si se llama manualmente)
        # Esto requeriría una tabla/columna adicional para rastrear recordatorios enviados.
        # Por simplicidad, omitimos esta verificación por ahora.

        reservations = conn.execute('''
            SELECT r.id as reservation_id, r.seat_number, c.email, c.first_name, c.last_name
            FROM reservations r
            JOIN customers c ON r.customer_id = c.id
            WHERE r.flight_id = ? AND r.status IN ('paid', 'confirmed')
        ''', (flight['id'],)).fetchall()

        for res in reservations:
            try:
                html_body = render_template('emails/flight_reminder_email.html',
                                            customer_name=f"{res['first_name']} {res['last_name']}",
                                            reservation_id=res['reservation_id'],
                                            flight=flight,
                                            seat_number=res['seat_number'],
                                            ticket_url=url_for('boarding_pass', reservation_id=res['reservation_id'], _external=True))
                send_email(subject=f"Recordatorio: Tu Vuelo {flight['flight_code']} está Próximo a Partir - UMES AIR",
                           sender_email=app.config['MAIL_USERNAME'],
                           recipients=[res['email']],
                           html_body=html_body)
                log_action(f"Correo de recordatorio de vuelo {flight['flight_code']} enviado a {res['email']} para reserva ID {res['reservation_id']}", "success")
                reminders_sent_count += 1
            except Exception as e:
                log_action(f"Error enviando correo de recordatorio de vuelo {flight['flight_code']} a {res['email']} (reserva ID {res['reservation_id']}): {e}", "error")
                errors_count += 1
    
    conn.close()
    # Esta ruta normalmente no sería accedida por un usuario, sino por un scheduler.
    # El flash message es más para depuración si se accede manualmente.
    flash(f'{reminders_sent_count} recordatorios de vuelo enviados. {errors_count} errores.', 'info' if errors_count == 0 else 'warning')
    return redirect(url_for('dashboard')) # O alguna página de admin

# ------- Authentication & Authorization -------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Por favor inicia sesión para acceder a esta página.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session or session['user_role'] not in allowed_roles:
                flash('No tienes permiso para acceder a esta página.', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ------- Routes -------
@app.before_request
def setup_before_first_request():
    global _is_first_request
    if _is_first_request:
        init_db()
        _is_first_request = False



@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = get_db_connection()
        # Temporalmente se elimina la comprobación de status para permitir el login si la BD no está actualizada.
        # TODO: Asegurarse de que la base de datos se recree con el schema.sql más reciente.
        user = conn.execute('SELECT * FROM users WHERE email = ? AND password = ?', 
                              (email, password_hash)).fetchone()
        conn.close()
        
        if user:
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['user_email'] = user['email']
            session['user_role'] = user['role']
            
            # Log la acción de inicio de sesión
            log_action(f"Usuario {user['id']} inició sesión", "success")
            
            flash(f'Bienvenido de nuevo, {user["name"]}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Correo o contraseña inválidos.', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    if 'user_id' in session:
        log_action(f"Usuario {session['user_id']} cerró sesión", "success")
        session.clear()
    return redirect(url_for('index'))

@app.route('/static/js/scripts.js')
def serve_js():
    """Sirve el código JavaScript generado por el archivo Python."""
    return Response(get_js(), mimetype='application/javascript')

@app.route('/dashboard')
@login_required
def dashboard():
    role = session['user_role']
    conn = get_db_connection()
    
    # Obtener los próximos 10 vuelos activos
    upcoming_flights = conn.execute('''
        SELECT f.*, COUNT(r.id) as booked_seats 
        FROM flights f
        LEFT JOIN reservations r ON f.id = r.flight_id AND r.status != 'cancelled'
        WHERE f.date >= date('now') AND f.status = 'active'
        GROUP BY f.id
        ORDER BY f.date, f.departure_time
        LIMIT 10
    ''').fetchall()
    
    # Para pasajeros, solo muestra sus reservas
    if role == 'passenger':
        reservations = conn.execute('''
            SELECT r.*, f.flight_code, f.origin, f.destination, f.date, f.departure_time, f.arrival_time
            FROM reservations r
            JOIN flights f ON r.flight_id = f.id
            WHERE r.customer_id = ?
            ORDER BY f.date, f.departure_time
        ''', (session['user_id'],)).fetchall()
    else:
        # Para administradores y agentes, muestra las reservas recientes
        reservations = conn.execute('''
            SELECT r.*, f.flight_code, f.origin, f.destination, f.date, f.departure_time, f.arrival_time,
                   c.first_name, c.last_name
            FROM reservations r
            JOIN flights f ON r.flight_id = f.id
            JOIN customers c ON r.customer_id = c.id
            ORDER BY r.created_at DESC
            LIMIT 10
        ''').fetchall()
    
    # Get counts for dashboard cards
    users_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    flights_count = conn.execute('SELECT COUNT(*) FROM flights WHERE status = "active"').fetchone()[0]
    reservations_count = conn.execute('SELECT COUNT(*) FROM reservations WHERE status = "paid"').fetchone()[0]
    customers_count = conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]

    conn.close()
    
    return render_template('dashboard.html', 
                          role=role, 
                          upcoming_flights=upcoming_flights, 
                          reservations=reservations,
                          users_count=users_count,
                          flights_count=flights_count,
                          reservations_count=reservations_count,
                          customers_count=customers_count)

@app.route('/payment/success/<reservation_id>')
@login_required
def payment_success(reservation_id):
    conn = get_db_connection()
    
    # Verificar que la reservación existe y pertenece al usuario actual
    reservation = conn.execute('''
        SELECT r.*, f.flight_code, f.origin, f.destination, f.date
        FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        WHERE r.id = ? AND r.customer_id = ?
    ''', (reservation_id, session['user_id'])).fetchone()
    
    if not reservation:
        conn.close()
        flash('Reservación no encontrada.', 'error')
        return redirect(url_for('dashboard'))
    
    # Obtener el pago asociado
    payment = conn.execute('SELECT * FROM payments WHERE reservation_id = ?', (reservation_id,)).fetchone()
    
    # Fetch full customer details for QR code and email
    customer = conn.execute('SELECT * FROM customers WHERE id = ?', (reservation['customer_id'],)).fetchone()

    conn.close()

    # Generate QR code (consistent with boarding_pass)
    qr_content = (
        f"RESERVATION_ID:{reservation['id']}; "
        f"FLIGHT:{reservation['flight_code']}; "
        f"PASSENGER:{customer['first_name'] if customer else ''} {customer['last_name'] if customer else ''}; "
        f"DATE:{reservation['date']}; "
        f"SEAT:{reservation['seat_number']}; "
        f"FROM:{reservation['origin']}; "
        f"TO:{reservation['destination']}"
    )
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=4,
    )
    qr.add_data(qr_content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    qr_code_image_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

    # Enviar correo de confirmación si aún no se ha enviado (ej. si el webhook falló o es un flujo diferente)
    # Esto es una salvaguarda, idealmente el webhook es la fuente primaria para pagos con Stripe.
    if reservation and reservation['status'] in ['paid', 'confirmed']:
        try:
            # Re-obtener detalles completos si es necesario, ya que 'reservation' aquí es limitado
            db_conn_for_email = get_db_connection()
            full_reservation_details = db_conn_for_email.execute('''
                SELECT r.*, f.flight_code, f.origin, f.destination, f.date, f.departure_time, f.total_fare,
                       c.first_name, c.last_name, c.email
                FROM reservations r
                JOIN flights f ON r.flight_id = f.id
                JOIN customers c ON r.customer_id = c.id
                WHERE r.id = ?
            ''', (reservation_id,)).fetchone()
            db_conn_for_email.close()

            if full_reservation_details:
                html_body = render_template('emails/confirmation_email.html',
                                            reservation=full_reservation_details,
                                            flight=full_reservation_details,
                                            customer=full_reservation_details,
                                            ticket_url=url_for('boarding_pass', reservation_id=reservation_id, _external=True))
                send_email(subject="Confirmación de Reserva - UMES AIR",
                           sender_email=app.config['MAIL_USERNAME'],
                           recipients=[full_reservation_details['email']],
                           html_body=html_body)
                log_action(f"Correo de confirmación (desde success page) enviado a {full_reservation_details['email']} para reserva {reservation_id}", "success")
            else:
                log_action(f"No se pudieron obtener detalles completos para el correo de confirmación (reserva {reservation_id}) desde success page", "warning")
        except Exception as e:
            log_action(f"Error enviando correo de confirmación (desde success page) para reserva {reservation_id}: {e}", "error")

    return render_template('reservations/success.html', 
                          reservation=reservation,
                          payment=payment,
                          qr_code_image=qr_code_image_base64)

@app.route('/payment/cancel/<reservation_id>')
@login_required
def payment_cancel(reservation_id):
    conn = get_db_connection()
    
    # Actualizar el estado de la reservación y el pago a 'cancelled'
    conn.execute('UPDATE reservations SET status = ? WHERE id = ?', ('cancelled', reservation_id))
    conn.execute('UPDATE payments SET status = ? WHERE reservation_id = ?', ('failed', reservation_id))
    conn.commit()
    conn.close()
    
    flash('La reservación ha sido cancelada.', 'info')
    return redirect(url_for('dashboard'))

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, stripe_keys['endpoint_secret']
        )
    except ValueError as e:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        stripe_session_data = event['data']['object'] # Renombrado para claridad
        reservation_id = stripe_session_data.get('client_reference_id')
        payment_intent_id = stripe_session_data.get('payment_intent') # Capturar payment_intent
        
        if reservation_id:
            conn = get_db_connection()
            try:
                # Actualizar el estado de la reservación y el pago
                # Usar 'paid' para consistencia con otros flujos de pago
                conn.execute("UPDATE reservations SET status = 'paid' WHERE id = ?", (reservation_id,))
                # Actualizar payment con transaction_id si existe la entrada, o crearla si no.
                payment_entry = conn.execute("SELECT id FROM payments WHERE reservation_id = ?", (reservation_id,)).fetchone()
                if payment_entry:
                    conn.execute("UPDATE payments SET status = 'completed', transaction_id = ?, updated_at = ? WHERE reservation_id = ?", 
                                 (payment_intent_id, datetime.now(), reservation_id))
                else:
                    # Si no existe entrada de pago (ej. si el flujo de creación de reserva no la creó antes)
                    # Necesitamos el monto. Asumimos que está en la reserva o se puede obtener.
                    reservation_amount = conn.execute("SELECT total_fare FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
                    if reservation_amount:
                        conn.execute("INSERT INTO payments (reservation_id, amount, payment_method, status, transaction_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                     (reservation_id, reservation_amount['total_fare'], 'stripe', 'completed', payment_intent_id, datetime.now(), datetime.now()))
                    else:
                        log_action(f"Webhook Stripe: No se pudo obtener el monto para la reserva {reservation_id} para crear entrada de pago.", "error")
                conn.commit()
                log_action(f"Webhook Stripe: Pago completado para reserva {reservation_id}, payment_intent: {payment_intent_id}", "success")

                # Obtener detalles para el correo
                reservation_details = conn.execute('''
                    SELECT r.*, f.flight_code, f.origin, f.destination, f.date, f.departure_time, f.total_fare,
                           c.first_name, c.last_name, c.email
                    FROM reservations r
                    JOIN flights f ON r.flight_id = f.id
                    JOIN customers c ON r.customer_id = c.id
                    WHERE r.id = ?
                ''', (reservation_id,)).fetchone()

                if reservation_details:
                    try:
                        html_body = render_template('emails/confirmation_email.html',
                                                    reservation=reservation_details,
                                                    flight=reservation_details, 
                                                    customer=reservation_details,
                                                    ticket_url=url_for('boarding_pass', reservation_id=reservation_id, _external=True))
                        send_email(subject="Confirmación de Reserva - UMES AIR",
                                   sender_email=app.config['MAIL_USERNAME'],
                                   recipients=[reservation_details['email']],
                                   html_body=html_body)
                        log_action(f"Correo de confirmación (webhook) enviado a {reservation_details['email']} para reserva {reservation_id}", "success")
                    except Exception as e:
                        log_action(f"Error enviando correo de confirmación (webhook) para reserva {reservation_id}: {e}", "error")
                else:
                    log_action(f"No se pudieron obtener detalles de reserva {reservation_id} para enviar correo desde webhook.", "warning")

            except Exception as e:
                if conn: # Asegurarse que conn existe antes de rollback
                    conn.rollback()
                log_action(f"Error al procesar pago para reservación {reservation_id} en webhook: {str(e)}", "error")
            finally:
                if conn: # Asegurarse que conn existe antes de cerrar
                    conn.close()

    return '', 200

# ------- User Management (Solo Admin) -------
@app.route('/users')
@login_required
@role_required(['admin'])
def list_users():
    conn = get_db_connection()
    users = conn.execute('SELECT * FROM users WHERE status = "active" ORDER BY role, name').fetchall()
    conn.close()
    return render_template('users/list.html', users=users)

@app.route('/users/new', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def new_user():
    if request.method == 'POST':
        user_id = str(uuid.uuid4())[:8]
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        role = request.form['role']
        
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = get_db_connection()
        try:
            conn.execute(
                'INSERT INTO users (id, name, email, password, role) VALUES (?, ?, ?, ?, ?)',
                (user_id, name, email, password_hash, role)
            )
            conn.commit()
            log_action(f"Admin creó nuevo usuario {role}: {email}", "success")
            flash(f'Usuario {name} creado con éxito!', 'success')
            return redirect(url_for('list_users'))
        except sqlite3.IntegrityError:
            flash('El correo electrónico ya existe.', 'error')
        finally:
            conn.close()
    customer_data = request.args.get('customer_data')
    if customer_data:
        customer_data = json.loads(customer_data)
    return render_template('users/new.html', customer_data=customer_data)


@app.route('/users/convert_from_customer/<customer_id>')
@login_required
@role_required(['admin', 'ticketing_agent'])
def convert_customer_to_user_form(customer_id):
    conn = get_db_connection()
    customer = conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    
    if not customer:
        conn.close()
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('list_customers'))

    # Verificar si ya existe un usuario activo con el mismo correo electrónico
    existing_user = conn.execute('SELECT * FROM users WHERE email = ? AND status = "active"', (customer['email'],)).fetchone()
    if existing_user:
        conn.close()
        flash(f'Ya existe un usuario activo con el correo electrónico {customer["email"]}.', 'error')
        return redirect(url_for('list_customers'))

    conn.close()
    # Preparar datos del cliente para pasarlos a la plantilla de nuevo usuario
    # El nombre de usuario será el nombre completo del cliente
    # El correo será el del cliente
    # Se deberá establecer una contraseña y un rol manualmente en el formulario
    customer_data = {
        'name': f"{customer['first_name']} {customer['last_name']}",
        'email': customer['email']
        # La contraseña y el rol se deben ingresar en el formulario
    }
    # Pasamos los datos como un string JSON en la URL para que new_user() los pueda leer
    return redirect(url_for('new_user', customer_data=json.dumps(customer_data)))

@app.route('/users/edit/<user_id>', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def edit_user(user_id):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE id = ? AND status = "active"', (user_id,)).fetchone()
    
    if not user:
        conn.close()
        flash('Usuario no encontrado.', 'error')
        return redirect(url_for('list_users'))
    
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        role = request.form['role']
        
        # Solo actualiza la contraseña si se proporciona
        if request.form['password']:
            password_hash = hashlib.sha256(request.form['password'].encode()).hexdigest()
            conn.execute(
                'UPDATE users SET name = ?, email = ?, password = ?, role = ? WHERE id = ?',
                (name, email, password_hash, role, user_id)
            )
        else:
            conn.execute(
                'UPDATE users SET name = ?, email = ?, role = ? WHERE id = ?',
                (name, email, role, user_id)
            )
        
        conn.commit()
        log_action(f"Admin actualizó usuario: {email}", "success")
        flash('Usuario actualizado con éxito!', 'success')
        return redirect(url_for('list_users'))
    
    conn.close()
    return render_template('users/edit.html', user=user)

@app.route('/users/delete/<user_id>', methods=['POST']) # Esta ruta se podría considerar para desactivar o para un borrado directo sin prompt
@login_required
@role_required(['admin'])
def delete_user_direct(user_id): # Renombrada para diferenciarla de la que tiene prompt
    if user_id == session['user_id']:
        flash('No puedes eliminar tu propia cuenta.', 'error') # Cambiado de desactivar a eliminar
        return redirect(url_for('list_users'))
    
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone() # Ya no se filtra por status active para poder eliminar inactivos
    
    if not user:
        conn.close()
        flash('Usuario no encontrado.', 'error')
        return redirect(url_for('list_users'))

    user_email = user['email']

    # Eliminar el usuario
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    log_action(f"Admin eliminó usuario: {user_email}", "success")

    # Verificar y eliminar el cliente asociado (si existe)
    customer_associated = conn.execute('SELECT id FROM customers WHERE email = ?', (user_email,)).fetchone()
    if customer_associated:
        conn.execute('DELETE FROM customers WHERE id = ?', (customer_associated['id'],))
        # También se deberían cancelar/eliminar las reservaciones asociadas a este cliente
        conn.execute('DELETE FROM reservations WHERE customer_id = ?', (customer_associated['id'],))
        log_action(f"Cliente asociado {user_email} eliminado junto con el usuario.", "success")
        flash(f'Usuario {user_email} y cliente asociado eliminados con éxito!', 'success')
    else:
        flash(f'Usuario {user_email} eliminado con éxito!', 'success')
    
    conn.commit()
    conn.close()
    return redirect(url_for('list_users'))

# La ruta original '/users/delete/<user_id>' ahora es delete_user_direct.
# La nueva ruta para el borrado con prompt es '/users/delete_with_customer_prompt/<user_id>'
# Se debe actualizar la plantilla users/list.html para que el botón de eliminar apunte a la nueva ruta con prompt.

@app.route('/users/delete_legacy/<user_id>', methods=['POST']) # Ruta original renombrada por si se quiere mantener la lógica de desactivación
@login_required
@role_required(['admin'])
def delete_user(user_id):
    if user_id == session['user_id']:
        flash('No puedes desactivar tu propia cuenta.', 'error')
        return redirect(url_for('list_users'))
    
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE id = ? AND status = "active"', (user_id,)).fetchone()
    
    if not user:
        conn.close()
        flash('Usuario no encontrado.', 'error')
        return redirect(url_for('list_users'))

    # Obtener el email del usuario para buscar el cliente asociado
    user_email = user['email']

    # Eliminar el usuario
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    log_action(f"Admin eliminó usuario: {user_email}", "success")

    # Verificar y eliminar el cliente asociado (si existe)
    customer_associated = conn.execute('SELECT id FROM customers WHERE email = ?', (user_email,)).fetchone()
    if customer_associated:
        conn.execute('DELETE FROM customers WHERE id = ?', (customer_associated['id'],))
        # También se deberían cancelar/eliminar las reservaciones asociadas a este cliente
        conn.execute('DELETE FROM reservations WHERE customer_id = ?', (customer_associated['id'],))
        log_action(f"Cliente asociado {user_email} eliminado junto con el usuario.", "success")
        flash(f'Usuario {user_email} y cliente asociado eliminados con éxito!', 'success')
    else:
        flash(f'Usuario {user_email} eliminado con éxito!', 'success')
    
    conn.commit()
    conn.close()
    return redirect(url_for('list_users'))

@app.route('/users/delete_with_customer_prompt/<user_id>', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def delete_user_with_customer_prompt(user_id):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    customer = conn.execute('SELECT * FROM customers WHERE email = ?', (user['email'],)).fetchone() if user else None

    if not user:
        conn.close()
        flash('Usuario no encontrado.', 'error')
        return redirect(url_for('list_users'))

    if request.method == 'POST':
        try:
            # Eliminar usuario
            conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
            log_action(f"Admin eliminó usuario: {user['email']}", "success", conn=conn)
            
            if customer and request.form.get('delete_customer'):
                conn.execute('DELETE FROM customers WHERE id = ?', (customer['id'],))
                # También se deberían cancelar/eliminar las reservaciones asociadas a este cliente
                conn.execute('DELETE FROM reservations WHERE customer_id = ?', (customer['id'],))
                log_action(f"Cliente asociado {customer['email']} eliminado junto con el usuario.", "success", conn=conn)
                flash(f'Usuario {user["email"]} y cliente asociado eliminados con éxito!', 'success')
            elif customer:
                flash(f'Usuario {user["email"]} eliminado. El cliente asociado no fue eliminado.', 'info')
            else:
                flash(f'Usuario {user["email"]} eliminado con éxito!', 'success')
            
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            flash(f'Error en la base de datos: {e}', 'error')
        finally:
            conn.close()
        return redirect(url_for('list_users'))

    reservations_customer_details = []
    if customer:
        reservations_customer_details = conn.execute('''
            SELECT r.id, r.created_at as reservation_date, r.status, f.flight_code as flight_number, f.origin, f.destination, f.date as flight_date
            FROM reservations r
            JOIN flights f ON r.flight_id = f.id
            WHERE r.customer_id = ? AND r.status NOT IN ('cancelled', 'completed')
        ''', (customer['id'],)).fetchall()

    conn.close()
    return render_template('users/confirm_delete.html', user=user, customer=customer, reservations_customer_details=reservations_customer_details)
    # Si el usuario es un 'passenger', su ID podría ser el customer_id.
    # Si es 'admin' o 'agent', es menos probable que tengan reservaciones directas, pero la lógica se aplica.

    # Buscamos el customer_id. Si el usuario es un cliente, su ID es el customer_id.
    # Si no, podríamos necesitar una lógica más compleja o asumir que no tienen reservaciones directas.
    # Para este caso, vamos a asumir que el user_id puede ser un customer_id.
    customer_id_to_check = user_id

    # Actualizar el estado de las reservaciones a 'cancelled'
    conn.execute("""
        UPDATE reservations 
        SET status = 'cancelled' 
        WHERE customer_id = ? AND status IN ('pending', 'paid')
    """, (customer_id_to_check,))
    
    conn.commit()
    conn.close()
    
    flash(f'Usuario {user["name"]} desactivado y sus reservaciones canceladas.', 'success')
    return redirect(url_for('list_users'))
    
    # Verificar si el usuario tiene reservas activas
    has_reservations = conn.execute('SELECT COUNT(*) as count FROM reservations WHERE customer_id = ?', 
                                  (user_id,)).fetchone()['count'] > 0
    
    if has_reservations and user['role'] == 'passenger':
        flash('No se puede eliminar un usuario con reservaciones activas.', 'error')
        conn.close()
        return redirect(url_for('list_users'))
    
    conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    log_action(f"Admin eliminó usuario: {user['email']}", "success")
    flash('Usuario eliminado con éxito!', 'success')
    conn.close()
    return redirect(url_for('list_users'))

# ------- Gestión de Vuelos (Solo Admin) -------
@app.route('/flights')
@login_required
def list_flights():
    conn = get_db_connection()
    
    # Obtener filtro de fecha (por defecto para hoy)
    filter_date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    
    flights = conn.execute('''
        SELECT f.*, 
               (SELECT COUNT(*) FROM reservations r WHERE r.flight_id = f.id AND r.status != 'cancelled') as booked_seats
        FROM flights f 
        WHERE date(f.date) = date(?)
        ORDER BY f.departure_time
    ''', (filter_date,)).fetchall()
    
    conn.close()
    return render_template('flights/list.html', flights=flights, filter_date=filter_date)

@app.route('/flights/new', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def new_flight():
    if request.method == 'POST':
        flight_code = request.form['flight_code']
        date = request.form['date']
        origin = request.form['origin']
        destination = request.form['destination']
        departure_time = request.form['departure_time']
        arrival_time = request.form['arrival_time']
        capacity = int(request.form['capacity'])
        base_fare = float(request.form.get('base_fare', 0))
        total_fare = float(request.form.get('total_fare', 0))
        
        if capacity > 18:
            flash('La capacidad máxima es de 18 asientos.', 'error')
            return render_template('flights/new.html')
        
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO flights (flight_code, date, origin, destination, departure_time, arrival_time, capacity, base_fare, total_fare)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (flight_code, date, origin, destination, departure_time, arrival_time, capacity, base_fare, total_fare))
            conn.commit()
            log_action(f"Admin creó nuevo vuelo: {flight_code} el {date}", "success")
            flash('Vuelo creado con éxito!', 'success')
            return redirect(url_for('list_flights', date=date))
        except sqlite3.IntegrityError:
            flash('El código de vuelo ya existe para esta fecha.', 'error')
        finally:
            conn.close()
    
    return render_template('flights/new.html')

@app.route('/flights/edit/<int:flight_id>', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def edit_flight(flight_id):
    conn = get_db_connection()
    flight = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
    
    if not flight:
        conn.close()
        flash('Vuelo no encontrado.', 'error')
        return redirect(url_for('list_flights'))
    
    # Obtener el número de reservaciones para este vuelo
    reserved_seats = conn.execute('SELECT COUNT(*) as count FROM reservations WHERE flight_id = ?', 
                                (flight_id,)).fetchone()['count']
    
    original_flight_date = flight['date']
    original_departure_time = flight['departure_time']

    if request.method == 'POST':
        flight_code = request.form['flight_code']
        new_date = request.form['date']
        origin = request.form['origin']
        destination = request.form['destination']
        new_departure_time = request.form['departure_time']
        arrival_time = request.form['arrival_time']
        capacity = int(request.form['capacity'])
        base_fare = float(request.form.get('base_fare', 0))
        total_fare = float(request.form.get('total_fare', 0))
        change_reason = request.form.get('change_reason', '') # Campo para la justificación

        if capacity > 18:
            flash('La capacidad máxima es de 18 asientos.', 'error')
            return render_template('flights/edit.html', flight=flight, reserved_seats=reserved_seats)
        
        if capacity < reserved_seats:
            flash(f'No se puede reducir la capacidad por debajo del número de asientos reservados ({reserved_seats}).', 'error')
            return render_template('flights/edit.html', flight=flight, reserved_seats=reserved_seats)
        
        date_or_time_changed = (original_flight_date != new_date) or (original_departure_time != new_departure_time)

        if date_or_time_changed and reserved_seats > 0 and not change_reason:
            flash('Debe proporcionar un motivo para el cambio de fecha/hora del vuelo si hay reservaciones existentes.', 'error')
            # Pasar los datos del formulario de vuelta para no perderlos
            current_form_data = request.form.to_dict()
            # Marcar que hubo este error para que la plantilla sepa que debe mostrar el campo de motivo
            request.form_had_change_reason_error = True 
            return render_template('flights/edit.html', flight=current_form_data, original_flight=flight, reserved_seats=reserved_seats, require_change_reason=True)

        try:
            conn.execute('''
                UPDATE flights 
                SET flight_code = ?, date = ?, origin = ?, destination = ?, 
                    departure_time = ?, arrival_time = ?, capacity = ?,
                    base_fare = ?, total_fare = ?, change_reason = ?
                WHERE id = ?
            ''', (flight_code, new_date, origin, destination, new_departure_time, arrival_time, capacity, 
                  base_fare, total_fare, change_reason if date_or_time_changed else flight['change_reason'], flight_id))
            conn.commit()
            log_action(f"Admin actualizó vuelo ID {flight_id}: {flight_code} el {new_date}. Razón cambio: {change_reason if date_or_time_changed else 'N/A'}", "success")
            flash('Vuelo actualizado con éxito!', 'success')

            if date_or_time_changed and reserved_seats > 0:
                # Obtener pasajeros afectados
                affected_reservations = conn.execute('''
                    SELECT c.email, c.first_name
                    FROM reservations r
                    JOIN customers c ON r.customer_id = c.id
                    WHERE r.flight_id = ? AND r.status IN ('pending', 'paid', 'confirmed')
                ''', (flight_id,)).fetchall()

                updated_flight_details = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                for res in affected_reservations:
                    try:
                        # Guardar el estado original del vuelo (antes del POST) para el correo
                        original_flight_details_for_email = {
                            'flight_code': flight['flight_code'],
                            'date': original_flight_date, # Usar la fecha original guardada
                            'origin': flight['origin'],
                            'destination': flight['destination'],
                            'departure_time': original_departure_time, # Usar la hora original guardada
                            'arrival_time': flight['arrival_time']
                        }
                        # The email sending logic for flight edit seems correct and already uses 'flight_change_email.html'.
                        # The previous error message was likely from the seat change functionality, which has been addressed by creating the missing template.
                        # No changes needed here for the template name.
                        html_body = render_template('emails/flight_change_email.html',
                                                    customer_name=res['first_name'],
                                                    original_flight=original_flight_details_for_email, 
                                                    new_flight=updated_flight_details, 
                                                    change_reason=change_reason,
                                                    now=now)
                        send_email(subject=f"Actualización Importante Sobre su Vuelo {updated_flight_details['flight_code']} - UMES AIR",
                                   sender_email=app.config['MAIL_DEFAULT_SENDER'],
                                   recipients=[res['email']],
                                   html_body=html_body)
                        log_action(f"Correo de cambio de vuelo enviado a {res['email']} para vuelo ID {flight_id} (Reserva ID: {res.get('reservation_id', 'N/A')})", "success")
                    except Exception as e:
                        log_action(f"Error enviando correo de cambio de vuelo a {res['email']} para vuelo ID {flight_id}: {e}", "error")
                        flash(f"Error al enviar correo de notificación a {res['email']}: {e}", "error")
            
            conn.close() # Cerrar conexión después de todas las operaciones de BD
            return redirect(url_for('list_flights', date=new_date))
        except sqlite3.IntegrityError:
            flash('El código de vuelo ya existe para esta fecha.', 'error')
            # No cerrar la conexión aquí si hay error, se cierra en el finally o al final de la ruta GET
    
    # Si es GET o hay error en POST y se renderiza de nuevo la plantilla, la conexión se cierra aquí
    if conn: # Asegurarse que la conexión no se haya cerrado ya
        conn.close()
    # Determinar si se requiere el motivo del cambio para pasarlo a la plantilla
    # Esto es True si hubo un intento de POST, se cambió fecha/hora, hay reservas y no se dio motivo.
    # En el caso de un GET inicial, no se requiere aún.
    require_reason_flag = False
    if request.method == 'POST' and hasattr(request, 'form_had_change_reason_error'):
        require_reason_flag = True 

    return render_template('flights/edit.html', flight=flight, reserved_seats=reserved_seats, original_flight=flight if request.method == 'GET' else current_form_data, require_change_reason=require_reason_flag)


@app.route('/flights/cancel_confirm/<int:flight_id>', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def cancel_flight_page(flight_id):
    # Paso 1: Obtener conexión y datos del vuelo
    with get_db_connection() as conn:
        flight = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
        if not flight:
            flash('Vuelo no encontrado.', 'error')
            return redirect(url_for('list_flights'))

        if request.method == 'POST':
            cancellation_reason = request.form.get('cancellation_reason')
            if not cancellation_reason:
                flash('Proporcione un motivo de cancelación.', 'error')
                return render_template('flights/cancel_flight_page.html', flight=flight)

            # Paso 2: Actualizar vuelo y reservas (transacción rápida)
            try:
                # Bloque de actualización rápida
                conn.execute('UPDATE flights SET status = ?, cancellation_reason = ? WHERE id = ?',
                            ('cancelled', cancellation_reason, flight_id))
                
                affected_reservations = conn.execute('''
                    SELECT r.id, c.email, c.first_name 
                    FROM reservations r
                    JOIN customers c ON r.customer_id = c.id
                    WHERE r.flight_id = ? AND r.status IN ('pending', 'paid', 'confirmed')
                ''', (flight_id,)).fetchall()

                # Actualizar reservas
                for res in affected_reservations:
                    conn.execute('UPDATE reservations SET status = ? WHERE id = ?',
                                ('cancelled', res['id']))
                
                # Commit INMEDIATO después de actualizaciones
                conn.commit()
            except Exception as e:
                conn.rollback()
                flash('Error al actualizar la base de datos.', 'error')
                return redirect(url_for('list_flights'))

            # Paso 3: Enviar correos FUERA de la transacción principal
            emails_sent_log = []
            for res in affected_reservations:
                try:
                    # Usar una NUEVA conexión para enviar correos y logging
                    with get_db_connection() as email_conn:
                        html_body = render_template('emails/flight_cancellation_email.html',
                                                  customer_name=res['first_name'],
                                                  flight=flight,
                                                  cancellation_reason=cancellation_reason,
                                                  now=datetime.now())
                        send_email(
                            subject=f"Cancelación de Vuelo - {flight['flight_code']}",
                            sender_email=app.config['MAIL_DEFAULT_SENDER'],
                            recipients=[res['email']],
                            html_body=html_body
                        )
                        # Loggear con conexión separada
                        log_action(f"Correo enviado a {res['email']}", "info", conn=email_conn)
                        emails_sent_log.append(res['email'])
                except Exception as e:
                    log_action(f"Error enviando correo: {str(e)}", "error")

            flash(f'Vuelo {flight["flight_code"]} cancelado. {"Notificaciones enviadas." if emails_sent_log else ""}', 'success')
            return redirect(url_for('list_flights'))

        return render_template('flights/cancel_flight_page.html', flight=flight)



@app.route('/flights/delete/<int:flight_id>', methods=['POST'])
@login_required
@role_required(['admin'])
def delete_flight(flight_id):
    # Esta ruta ahora solo redirige a la nueva página de confirmación para GET, 
    # o maneja la lógica si se llama directamente con POST (aunque no debería ser el caso desde la UI)
    # Para mantener la compatibilidad si algo aún llama a esta ruta directamente con POST:
    conn = get_db_connection()
    flight = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
    
    if not flight:
        conn.close()
        flash('Vuelo no encontrado.', 'error')
        return redirect(url_for('list_flights'))

    cancellation_reason = request.form.get('cancellation_reason')
    if not cancellation_reason:
        flash('Debe proporcionar un motivo para la cancelación del vuelo.', 'error')
        conn.close() # Asegurarse de cerrar la conexión
        # Redirigir a la página de confirmación para que el usuario pueda ingresar el motivo
        return redirect(url_for('cancel_flight_page', flight_id=flight_id))

    # Lógica de cancelación (duplicada de cancel_flight_page por si se llama directamente)
    affected_reservations = conn.execute('''
        SELECT r.id as reservation_id, r.status, c.email as customer_email, c.first_name as customer_name
        FROM reservations r
        JOIN customers c ON r.customer_id = c.id
        WHERE r.flight_id = ? AND r.status IN ('pending', 'paid', 'confirmed')
    ''', (flight_id,)).fetchall()

    conn.execute('UPDATE flights SET status = ?, cancellation_reason = ? WHERE id = ?', 
                 ('cancelled', cancellation_reason, flight_id))
    
    emails_sent_log = []
    now_dt = datetime.now()

    for res in affected_reservations:
        conn.execute('UPDATE reservations SET status = ? WHERE id = ?', 
                     ('cancelled', res['reservation_id']))
        
        try:
            html_body = render_template('emails/flight_cancellation_email.html',
                                        customer_name=res['customer_name'],
                                        flight=flight, 
                                        cancellation_reason=cancellation_reason,
                                        now=now_dt)
            send_email(subject=f"Notificación de Cancelación de Vuelo - {flight['flight_code']} - UMES AIR",
                           sender_email=app.config['MAIL_DEFAULT_SENDER'],
                           recipients=[res['customer_email']],
                           html_body=html_body)
            log_action(f"Correo de cancelación de vuelo {flight['flight_code']} enviado a {res['customer_email']} para reserva ID {res['reservation_id']}", "success")
            emails_sent_log.append(res['customer_email'])
        except Exception as e:
                log_action(f"Error enviando correo de cancelación de vuelo {flight['flight_code']} a {res['customer_email']} (reserva ID {res['reservation_id']}): {e}", "error")

        conn.commit()
        log_action(f"Admin canceló vuelo ID: {flight_id} ({flight['flight_code']}) por: {cancellation_reason}. Notificaciones enviadas a: {', '.join(emails_sent_log) if emails_sent_log else 'ningún cliente'}.", "warning")
        flash(f'Vuelo {flight["flight_code"]} cancelado, reservaciones asociadas actualizadas y notificaciones enviadas.', 'success')
        conn.close()
        return redirect(url_for('list_flights'))

    # Para el método GET, simplemente mostrar la página de confirmación
    conn.close()
    return render_template('flights/cancel_flight_page.html', flight=flight)

# ------- Gestión de Clientes -------
@app.route('/customers')
@login_required
@role_required(['admin', 'agent'])
def list_customers():
    conn = get_db_connection()
    customers = conn.execute('SELECT * FROM customers ORDER BY last_name, first_name').fetchall()
    conn.close()
    return render_template('customers/list.html', customers=customers)

@app.route('/customers/new', methods=['GET', 'POST'])
@login_required
@role_required(['admin', 'agent'])
def new_customer():
    if request.method == 'POST':
        customer_id = str(uuid.uuid4())[:8]
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        nationality = request.form['nationality']
        dob = request.form['dob']
        gender = request.form['gender']
        id_number = request.form['id_number']
        email = request.form['email']
        phone = request.form['phone']
        emergency_contact = request.form['emergency_contact']
        
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO customers (id, first_name, last_name, nationality, date_of_birth, gender, 
                                      id_number, email, phone, emergency_contact)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (customer_id, first_name, last_name, nationality, dob, gender, 
                 id_number, email, phone, emergency_contact))
            conn.commit()
            log_action(f"Usuario {session['user_id']} creó nuevo cliente: {customer_id}", "success")
            flash('Cliente creado con éxito!', 'success')
            return redirect(url_for('list_customers'))
        except sqlite3.IntegrityError:
            flash('Ya existe un cliente con este ID o correo electrónico.', 'error')
        finally:
            conn.close()
    
    return render_template('customers/new.html')

@app.route('/customers/edit/<customer_id>', methods=['GET', 'POST'])
@login_required
@role_required(['admin', 'agent'])
def edit_customer(customer_id):
    conn = get_db_connection()
    customer = conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    
    if not customer:
        conn.close()
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('list_customers'))
    
    if request.method == 'POST':
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        nationality = request.form['nationality']
        dob = request.form['dob']
        gender = request.form['gender']
        id_number = request.form['id_number']
        email = request.form['email']
        phone = request.form['phone']
        emergency_contact = request.form['emergency_contact']
        
        try:
            conn.execute('''
                UPDATE customers 
                SET first_name = ?, last_name = ?, nationality = ?, date_of_birth = ?, gender = ?, 
                    id_number = ?, email = ?, phone = ?, emergency_contact = ?
                WHERE id = ?
            ''', (first_name, last_name, nationality, dob, gender, 
                 id_number, email, phone, emergency_contact, customer_id))
            conn.commit()
            log_action(f"Usuario {session['user_id']} actualizó cliente: {customer_id}", "success")
            flash('Cliente actualizado con éxito!', 'success')
            return redirect(url_for('list_customers'))
        except sqlite3.IntegrityError:
            flash('El número de identificación o correo electrónico ya existe.', 'error')
    
    conn.close()
    return render_template('customers/edit.html', customer=customer)

@app.route('/customers/delete_prompt/<customer_id>', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def delete_customer_with_user_prompt(customer_id):
    conn = get_db_connection()
    customer = conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()

    if not customer:
        flash('Cliente no encontrado.', 'error')
        conn.close()
        return redirect(url_for('list_customers'))

    user_associated = conn.execute('SELECT * FROM users WHERE email = ?', (customer['email'],)).fetchone()

    reservations_customer_details = conn.execute('''
        SELECT r.id, f.flight_code, f.date, f.origin, f.destination, r.seat_number
        FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        WHERE r.customer_id = ? AND r.status NOT IN ('cancelled', 'completed') AND f.status = 'active'
    ''', (customer_id,)).fetchall()

    reservations_user_details = []
    if user_associated:
        reservations_user_details = conn.execute('''
            SELECT r.id, f.flight_code, f.date, f.origin, f.destination, r.seat_number
            FROM reservations r
            JOIN flights f ON r.flight_id = f.id
            WHERE r.customer_id = ? AND r.status NOT IN ('cancelled', 'completed') AND f.status = 'active'
        ''', (user_associated['id'],)).fetchall()

    if request.method == 'POST':
        try:
            log_action(f"Intento de eliminación para cliente ID: {customer_id} por usuario {session['user_id']}", "info", conn=conn)

            delete_associated_user = 'delete_user' in request.form

            if delete_associated_user and user_associated:
                active_reservations_user = conn.execute('SELECT COUNT(*) FROM reservations WHERE customer_id = ? AND status NOT IN ("cancelled", "completed") AND flight_id IN (SELECT id FROM flights WHERE status = "active")' , (user_associated['id'],)).fetchone()[0]
                if active_reservations_user > 0:
                    flash(f"No se puede eliminar el usuario asociado {user_associated['email']} porque tiene reservaciones activas.", 'error')
                    conn.rollback()
                    return redirect(url_for('list_customers'))
                
                conn.execute('DELETE FROM reservations WHERE customer_id = ?', (user_associated['id'],))
                log_action(f"Reservaciones del usuario asociado {user_associated['email']} eliminadas.", "warning", conn=conn)
                
                conn.execute('DELETE FROM users WHERE id = ?', (user_associated['id'],))
                log_action(f"Usuario asociado {user_associated['email']} eliminado.", "warning", conn=conn)
                flash(f'Usuario asociado {user_associated["email"]} y sus reservaciones eliminados con éxito!', 'success')

            active_reservations_customer = conn.execute('SELECT COUNT(*) FROM reservations WHERE customer_id = ? AND status NOT IN ("cancelled", "completed") AND flight_id IN (SELECT id FROM flights WHERE status = "active")' , (customer['id'],)).fetchone()[0]


            if not (delete_associated_user and user_associated and user_associated['id'] == customer['id']):
                conn.execute('DELETE FROM reservations WHERE customer_id = ?', (customer_id,))
                log_action(f"Reservaciones del cliente {customer['email']} eliminadas.", "warning", conn=conn)

            conn.execute('DELETE FROM customers WHERE id = ?', (customer_id,))
            conn.commit()
            log_action(f"Cliente {customer['email']} eliminado por {session['user_id']}.", "success", conn=conn)
            flash('Cliente eliminado con éxito!', 'success')
            return redirect(url_for('list_customers'))

        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            log_action(f"Error al eliminar cliente {customer_id} o usuario asociado: {str(e)}", "error", conn=conn)
            flash(f'Ocurrió un error durante la eliminación: {str(e)}', 'error')
            return redirect(url_for('list_customers'))
        finally:
            if conn:
                conn.close()

    conn.close()
    return render_template('customers/confirm_delete.html', 
                           customer=customer, 
                           user_associated=user_associated,
                           reservations_customer=reservations_customer_details,
                           reservations_user=reservations_user_details)


# ------- Gestión de Reservaciones -------
@app.route('/flights/<int:flight_id>/seats', methods=['GET', 'POST'])
@login_required
def flight_seats(flight_id):
    conn = get_db_connection()
    flight = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
    
    if not flight:
        conn.close()
        flash('Vuelo no encontrado.', 'error')
        return redirect(url_for('list_flights'))
    
    # Obtener asientos ocupados
    reservations = conn.execute('''
        SELECT r.seat_number, c.first_name, c.last_name
        FROM reservations r
        JOIN customers c ON r.customer_id = c.id
        WHERE r.flight_id = ? AND r.status != 'cancelled'
    ''', (flight_id,)).fetchall()
    
    occupied_seats = {r['seat_number']: f"{r['first_name']} {r['last_name']}" for r in reservations}
    
    # Verificar si el usuario actual tiene una reserva para este vuelo
    user_reservation = None
    customer_data = None
    
    if session['user_role'] == 'passenger':
        user_reservation = conn.execute('''
            SELECT * FROM reservations
            WHERE flight_id = ? AND customer_id = ? AND status != 'cancelled'
        ''', (flight_id, session['user_id'])).fetchone()
        
        # Obtener datos del cliente si es un pasajero
        customer_data = conn.execute('''
            SELECT * FROM customers
            WHERE id = ?
        ''', (session['user_id'],)).fetchone()
    
    if request.method == 'POST':
        seat_number = request.form.get('seat_number')
        
        if not seat_number:
            flash('Por favor seleccione un asiento.', 'error')
            return redirect(url_for('flight_seats', flight_id=flight_id))
        
        if seat_number in occupied_seats:
            flash('Este asiento ya está ocupado.', 'error')
            return redirect(url_for('flight_seats', flight_id=flight_id))
        
        # Crear una reservación temporal
        reservation_id = str(uuid.uuid4())[:8]
        price = 450.00  # Precio base del vuelo
        
        try:
            # Crear sesión de pago con Stripe
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'unit_amount': int(price * 100),  # Stripe requiere el monto en centavos
                        'product_data': {
                            'name': f'Vuelo {flight["flight_code"]} - Asiento {seat_number}',
                            'description': f'Vuelo de {flight["origin"]} a {flight["destination"]} el {flight["date"]}'
                        },
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=request.host_url.rstrip('/') + url_for('payment_success', reservation_id=reservation_id),
                cancel_url=request.host_url.rstrip('/') + url_for('payment_cancel', reservation_id=reservation_id),
                client_reference_id=reservation_id,
                customer_email=customer_data['email'] if customer_data else None
            )
            
            # Guardar la reservación como pendiente
            conn.execute('''
                INSERT INTO reservations (id, flight_id, customer_id, seat_number, price, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (reservation_id, flight_id, session['user_id'], seat_number, price, 'pending', datetime.now()))
            
            # Crear registro de pago pendiente
            payment_id = str(uuid.uuid4())[:8]
            conn.execute('''
                INSERT INTO payments (id, reservation_id, amount, payment_method, status, stripe_payment_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (payment_id, reservation_id, price, 'card', 'pending', checkout_session.id))
            
            conn.commit()
            
            # Redirigir a la página de pago de Stripe
            return redirect(checkout_session.url)
            
        except Exception as e:
            conn.rollback()
            flash('Error al procesar la reservación. Por favor intente de nuevo.', 'error')
            return redirect(url_for('flight_seats', flight_id=flight_id))
        finally:
            conn.close()
    
    conn.close()
    
    # Crear la estructura de asientos mostrando todos los asientos (18)
    seats = []
    total_rows = 6  # Total de filas para 18 asientos
    available_seats = flight['capacity']  # Asientos disponibles según la capacidad configurada
    
    for row in range(1, total_rows + 1):
        row_seats = []
        for col in ['A', 'B', 'C']:
            seat_number = f"{row}{col}"
            # Marcar asientos como no disponibles si exceden la capacidad configurada
            if (row - 1) * 3 + (['A', 'B', 'C'].index(col) + 1) > available_seats:
                occupied_seats[seat_number] = 'No disponible'
            row_seats.append(seat_number)
        seats.append(row_seats)
    
    return render_template('reservations/seats.html', 
                          flight=flight, 
                          seats=seats, 
                          occupied_seats=occupied_seats,
                          user_reservation=user_reservation)

@app.route('/reservations/new/<flight_id>', methods=['GET', 'POST'])
@login_required
@role_required(['admin', 'agent'])
def new_reservation(flight_id):
    conn = get_db_connection()
    flight = conn.execute("SELECT * FROM flights WHERE id = ? AND status = 'active'", (flight_id,)).fetchone()
    
    if not flight:
        conn.close()
        flash('Vuelo no encontrado o no está activo. No se pueden crear nuevas reservaciones.', 'error')
        return redirect(url_for('list_flights'))
    
    if request.method == 'POST':
        customer_id = request.form.get('customer_id')
        if not customer_id:
            flash('Por favor seleccione un cliente.', 'error')
            return redirect(url_for('new_reservation', flight_id=flight_id))
        
        return redirect(url_for('reservation_step2_seat', flight_id=flight_id, customer_id=customer_id))
    
    # Obtener todos los clientes para el selector
    customers = conn.execute('SELECT * FROM customers ORDER BY last_name, first_name').fetchall()
    conn.close()
    
    return render_template('reservations/step1_select_customer.html', 
                          flight=flight,
                          customers=customers)

@app.route('/reservations/step2/<flight_id>/<customer_id>', methods=['GET'])
@login_required
@role_required(['admin', 'agent'])
def reservation_step2_seat(flight_id, customer_id):
    conn = get_db_connection()
    flight = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
    customer = conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    
    if not flight or not customer:
        conn.close()
        flash('Información no encontrada.', 'error')
        return redirect(url_for('list_flights'))
    
    # Verificar si este cliente ya tiene una reserva para este vuelo
    existing_reservation = conn.execute('''
        SELECT * FROM reservations
        WHERE flight_id = ? AND customer_id = ? AND status != 'cancelled'
    ''', (flight_id, customer_id)).fetchone()
    
    # Si existe una reservación, estamos en modo de cambio de asiento
    user_reservation = existing_reservation if existing_reservation else None
    
    # Solo verificar límite de reservaciones si no existe una reservación (nueva reservación)
    if not user_reservation:
        flight_date = flight['date']
        
        # Contar reservaciones de ida (mismo origen-destino) para este cliente en esta fecha
        outbound_count = conn.execute('''
            SELECT COUNT(*) as count FROM reservations r
            JOIN flights f ON r.flight_id = f.id
            WHERE r.customer_id = ? AND f.date = ? 
            AND f.origin = ? AND f.destination = ? 
            AND r.status != 'cancelled'
        ''', (customer_id, flight_date, flight['origin'], flight['destination'])).fetchone()['count']
        
        # Contar reservaciones de vuelta (destino-origen invertido) para este cliente en esta fecha
        inbound_count = conn.execute('''
            SELECT COUNT(*) as count FROM reservations r
            JOIN flights f ON r.flight_id = f.id
            WHERE r.customer_id = ? AND f.date = ? 
            AND f.origin = ? AND f.destination = ? 
            AND r.status != 'cancelled'
        ''', (customer_id, flight_date, flight['destination'], flight['origin'])).fetchone()['count']
        
        # Determinar si este vuelo es de ida o vuelta
        is_outbound = True  # Por defecto asumimos que es un vuelo de ida
        
        # Si hay reservaciones en la dirección opuesta, entonces este es un vuelo de vuelta
        if inbound_count > 0:
            is_outbound = False
        
        # Verificar si ya alcanzó el límite según la dirección
        if (is_outbound and outbound_count > 0):
            flash('Este cliente ya tiene una reservación de ida para esta fecha.', 'error')
            return redirect(url_for('new_reservation', flight_id=flight_id))
        elif (not is_outbound and inbound_count > 0):
            flash('Este cliente ya tiene una reservación de vuelta para esta fecha.', 'error')
            return redirect(url_for('new_reservation', flight_id=flight_id))
    
    # Obtener asientos ocupados
    occupied_seats = conn.execute('''
        SELECT seat_number FROM reservations
        WHERE flight_id = ? AND status != 'cancelled'
    ''', (flight_id,)).fetchall()
    
    occupied_seat_numbers = [seat['seat_number'] for seat in occupied_seats]
    conn.close()
    
    # Crear la estructura de asientos mostrando todos los asientos (18)
    seats = []
    total_rows = 6  # Total de filas para 18 asientos
    available_seats = flight['capacity']  # Asientos disponibles según la capacidad configurada
    
    for row in range(1, total_rows + 1):
        row_seats = []
        for col in ['A', 'B', 'C']:
            seat_number = f"{row}{col}"
            # Marcar asientos como no disponibles si exceden la capacidad configurada
            if (row - 1) * 3 + (['A', 'B', 'C'].index(col) + 1) > available_seats:
                occupied_seat_numbers.append(seat_number)
            row_seats.append(seat_number)
        seats.append(row_seats)
    
    # Convertir lista de asientos ocupados a diccionario para compatibilidad con el template
    occupied_seats = {seat: 'Ocupado' for seat in occupied_seat_numbers}
    
    return render_template('reservations/seats.html',
                          flight=flight,
                          customer=customer,
                          seats=seats,
                          occupied_seats=occupied_seats,
                          user_reservation=user_reservation)
@app.route('/reservations/step2/<flight_id>/<customer_id>/select-seat', methods=['POST'])
@login_required
@role_required(['admin', 'agent'])
def reservation_step2_select_seat(flight_id, customer_id):
    conn = get_db_connection()
    flight = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
    customer = conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    
    if not flight or not customer:
        conn.close()
        flash('Información no encontrada.', 'error')
        return redirect(url_for('list_flights'))
    
    seat_number = request.form.get('new_seat')
    if not seat_number:
        flash('Por favor seleccione un asiento.', 'error')
        return redirect(url_for('reservation_step2_seat', flight_id=flight_id, customer_id=customer_id))
    
    # Verificar si el asiento ya está ocupado
    occupied_seat = conn.execute('''
        SELECT id FROM reservations
        WHERE flight_id = ? AND seat_number = ? AND status != 'cancelled'
    ''', (flight_id, seat_number)).fetchone()
    
    if occupied_seat:
        flash('El asiento seleccionado ya está ocupado.', 'error')
        return redirect(url_for('reservation_step2_seat', flight_id=flight_id, customer_id=customer_id))
    
    # Verificar límite de reservaciones por día y dirección
    flight_date = flight['date']
    
    # Verificar si es un vuelo de ida (origen-destino) o vuelta (destino-origen)
    # Contar reservaciones de ida para este cliente en esta fecha
    outbound_count = conn.execute('''
        SELECT COUNT(*) as count FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        WHERE r.customer_id = ? AND f.date = ? 
        AND f.origin = ? AND f.destination = ? 
        AND r.status != 'cancelled'
    ''', (customer_id, flight_date, flight['origin'], flight['destination'])).fetchone()['count']
    
    # Contar reservaciones de vuelta para este cliente en esta fecha
    inbound_count = conn.execute('''
        SELECT COUNT(*) as count FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        WHERE r.customer_id = ? AND f.date = ? 
        AND f.origin = ? AND f.destination = ? 
        AND r.status != 'cancelled'
    ''', (customer_id, flight_date, flight['destination'], flight['origin'])).fetchone()['count']
    
    # Determinar si este vuelo es de ida o vuelta
    is_outbound = True  # Por defecto asumimos que es un vuelo de ida
    
    # Si hay reservaciones en la dirección opuesta, entonces este es un vuelo de vuelta
    if inbound_count > 0:
        is_outbound = False
    
    # Verificar si ya alcanzó el límite según la dirección
    if (is_outbound and outbound_count > 0):
        flash('Este cliente ya tiene una reservación de ida para esta fecha.', 'error')
        return redirect(url_for('new_reservation', flight_id=flight_id))
    elif (not is_outbound and inbound_count > 0):
        flash('Este cliente ya tiene una reservación de vuelta para esta fecha.', 'error')
        return redirect(url_for('new_reservation', flight_id=flight_id))
    
    conn.close()
    return redirect(url_for('reservation_step3_details',
                           flight_id=flight_id,
                           customer_id=customer_id,
                           seat_number=seat_number))

@app.route('/reservations/step3/<flight_id>/<customer_id>/<seat_number>', methods=['GET', 'POST'])
@login_required
@role_required(['admin', 'agent'])
def reservation_step3_details(flight_id, customer_id, seat_number):
    conn = get_db_connection()
    flight = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
    customer = conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    
    if not flight or not customer:
        conn.close()
        flash('Información no encontrada.', 'error')
        return redirect(url_for('list_flights'))
    
    if request.method == 'POST':
        # Verificar nuevamente el límite de reservaciones por día
        flight_date = flight['date']
        
        # Contar reservaciones de ida (mismo origen-destino) para este cliente en esta fecha
        outbound_count = conn.execute('''
            SELECT COUNT(*) as count FROM reservations r
            JOIN flights f ON r.flight_id = f.id
            WHERE r.customer_id = ? AND f.date = ? 
            AND f.origin = ? AND f.destination = ? 
            AND r.status != 'cancelled'
        ''', (customer_id, flight_date, flight['origin'], flight['destination'])).fetchone()['count']
        
        # Contar reservaciones de vuelta (destino-origen invertido) para este cliente en esta fecha
        inbound_count = conn.execute('''
            SELECT COUNT(*) as count FROM reservations r
            JOIN flights f ON r.flight_id = f.id
            WHERE r.customer_id = ? AND f.date = ? 
            AND f.origin = ? AND f.destination = ? 
            AND r.status != 'cancelled'
        ''', (customer_id, flight_date, flight['destination'], flight['origin'])).fetchone()['count']
        
        # Determinar si este vuelo es de ida o vuelta
        is_outbound = True  # Por defecto asumimos que es un vuelo de ida
        
        # Si hay reservaciones en la dirección opuesta, entonces este es un vuelo de vuelta
        if inbound_count > 0:
            is_outbound = False
        
        # Verificar si ya alcanzó el límite según la dirección
        if (is_outbound and outbound_count > 0):
            flash('Este cliente ya tiene una reservación de ida para esta fecha.', 'error')
            return redirect(url_for('new_reservation', flight_id=flight_id))
        elif (not is_outbound and inbound_count > 0):
            flash('Este cliente ya tiene una reservación de vuelta para esta fecha.', 'error')
            return redirect(url_for('new_reservation', flight_id=flight_id))
        
        # Actualizar información del cliente
        first_name = request.form.get('first_name')
        last_name = request.form.get('last_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        observations = request.form.get('observations', '')
        
        conn.execute('''
            UPDATE customers
            SET first_name = ?, last_name = ?, email = ?, phone = ?
            WHERE id = ?
        ''', (first_name, last_name, email, phone, customer_id))
        
        # Crear la reserva en estado pendiente
        reservation_id = str(uuid.uuid4())[:8]
        conn.execute('''
            INSERT INTO reservations (id, flight_id, customer_id, seat_number, price, status, observations, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, datetime('now'))
        ''', (reservation_id, flight_id, customer_id, seat_number, flight['total_fare'], observations))

        
        conn.commit()
        conn.close()
        return redirect(url_for('reservation_step4_payment', reservation_id=reservation_id))
    
    conn.close()
    return render_template('reservations/step3_details.html',
                          flight=flight,
                          customer=customer,
                          seat_number=seat_number)

@app.route('/reservations/step4/<reservation_id>', methods=['GET'])
@login_required
@role_required(['admin', 'agent'])
def reservation_step4_payment(reservation_id):
    conn = get_db_connection()
    reservation = conn.execute('''
        SELECT r.*, f.*, c.*,
               f.id as flight_id, f.flight_code, f.date, f.origin, f.destination,
               f.departure_time, f.arrival_time, f.base_fare, f.total_fare,
               c.id as customer_id, c.first_name, c.last_name
        FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        WHERE r.id = ? AND r.status = 'pending'
    ''', (reservation_id,)).fetchone()
    
    if not reservation:
        conn.close()
        flash('Reserva no encontrada o ya procesada.', 'error')
        return redirect(url_for('list_flights'))
    
    conn.close()

    
    
    return render_template('reservations/step4_payment.html',
                          reservation=reservation,
                          flight=reservation,
                          customer=reservation,
                          selected_seat=reservation['seat_number'])

@app.route('/reservations/<reservation_id>')
@login_required
def view_reservation(reservation_id):
    conn = get_db_connection()
    
    #Obtener la reservación y los detalles del vuelo y el cliente
    reservation = conn.execute('''
        SELECT r.*, f.flight_code, f.date, f.origin, f.destination, f.departure_time, f.arrival_time,
               c.first_name, c.last_name, c.id_number, c.email, c.phone
        FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        WHERE r.id = ?
    ''', (reservation_id,)).fetchone()
    
    if not reservation:
        conn.close()
        flash('Reservación no encontrada.', 'error')
        return redirect(url_for('dashboard'))
    
    # Verificar si el usuario puede ver esta reservación
    if session['user_role'] == 'passenger' and reservation['customer_id'] != session['user_id']:
        conn.close()
        flash('No tienes permiso para ver esta reservación.', 'error')
        return redirect(url_for('dashboard'))
    
    conn.close()
    
    return render_template('reservations/view.html', reservation=reservation)

@app.route('/reservations/<reservation_id>/change-seat', methods=['POST'])
@login_required
def change_seat(reservation_id):
    new_seat = request.form['new_seat']
    
    with get_db_connection() as conn:
        # Obtener la reservacion
        reservation = conn.execute('SELECT * FROM reservations WHERE id = ?', (reservation_id,)).fetchone()
        
        if not reservation:
            flash('Reservación no encontrada.', 'error')
            return redirect(url_for('dashboard'))
        
        # Verificar si el usuario puede modificar esta reservación
        if session['user_role'] == 'passenger' and reservation['customer_id'] != session['user_id']:
            flash('No tienes permiso para modificar esta reservación.', 'error')
            return redirect(url_for('dashboard'))
        
        # Verificar si el asiento ya está ocupado
        is_occupied = conn.execute('''
            SELECT 1 FROM reservations 
            WHERE flight_id = ? AND seat_number = ? AND status != 'cancelled' AND id != ?
        ''', (reservation['flight_id'], new_seat, reservation_id)).fetchone() is not None
        
        if is_occupied:
            flash(f'Asiento {new_seat} ya está ocupado.', 'error')
            return redirect(url_for('view_reservation', reservation_id=reservation_id))
        
        # Actualizar el asiento en la base de datos
        conn.execute('''
            UPDATE reservations SET seat_number = ? WHERE id = ?
        ''', (new_seat, reservation_id))
        conn.commit()
        
        # Obtener detalles del cliente y del vuelo para el correo
        customer = conn.execute('SELECT email, first_name FROM customers WHERE id = ?', (reservation['customer_id'],)).fetchone()
        flight = conn.execute('SELECT flight_code, date, origin, destination, departure_time FROM flights WHERE id = ?', (reservation['flight_id'],)).fetchone()

        if customer and flight:
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                html_body = render_template('emails/seat_change_email.html',
                                            customer_name=customer['first_name'],
                                            flight=flight,
                                            flight_code=flight['flight_code'],
                                            flight_date=flight['date'],
                                            origin=flight['origin'],
                                            destination=flight['destination'],
                                            old_seat=reservation['seat_number'],
                                            new_seat_number=new_seat,
                                            now=now)
                send_email(subject=f"Confirmación de Cambio de Asiento - Vuelo {flight['flight_code']} - UMES AIR",
                           sender_email=app.config['MAIL_DEFAULT_SENDER'],
                           recipients=[customer['email']],
                           html_body=html_body)
                log_action(f"Correo de cambio de asiento enviado a {customer['email']} para reserva {reservation_id}", "success")
                flash(f'Asiento actualizado a {new_seat}! Se ha enviado una notificación por correo.', 'success')
            except Exception as e:
                error_message = f"Error enviando correo de cambio de asiento para reserva {reservation_id}: {str(e)}"
                print(f"DEBUG: {error_message}") # Imprimir el error en la consola del servidor
                log_action(error_message, "error")
                flash(f'Asiento actualizado a {new_seat}, pero hubo un error al enviar la notificación por correo: {str(e)}', 'danger')
        else:
            flash(f'Asiento actualizado a {new_seat}, pero no se pudo enviar la notificación por correo (faltan datos).', 'warning')
            log_action(f"No se pudo enviar correo de cambio de asiento para reserva {reservation_id} por falta de datos de cliente/vuelo", "error")

        return redirect(url_for('view_reservation', reservation_id=reservation_id))

@app.route('/reservations/<reservation_id>/pay_manual', methods=['POST'])
@login_required
@role_required(['admin', 'agent'])
def pay_reservation_manual(reservation_id):
    conn = get_db_connection()
    reservation = conn.execute('''
        SELECT r.*, f.flight_code, f.origin, f.destination, f.date, f.departure_time, f.total_fare,
               c.first_name, c.last_name, c.email
        FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        WHERE r.id = ?
    ''', (reservation_id,)).fetchone()

    if not reservation:
        conn.close()
        flash('Reservación no encontrada.', 'error')
        return redirect(url_for('dashboard'))

    if reservation['status'] == 'paid':
        conn.close()
        flash('Esta reservación ya ha sido pagada.', 'info')
        return redirect(url_for('view_reservation', reservation_id=reservation_id))

    # Actualizar status para pago
    conn.execute("UPDATE reservations SET status = 'paid' WHERE id = ?", (reservation_id,))
    # Crear registro de pago simulado
    payment_entry = conn.execute("SELECT id FROM payments WHERE reservation_id = ?", (reservation_id,)).fetchone()
    if payment_entry:
        conn.execute("UPDATE payments SET status = 'completed', payment_method = ?, transaction_id = ?, updated_at = ? WHERE id = ?",
                     ('cash_or_manual_card', f'manual_{reservation_id}_{int(time.time())}', datetime.now(), payment_entry['id']))
    else:
        conn.execute("INSERT INTO payments (reservation_id, amount, payment_method, status, transaction_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (reservation_id, reservation['total_fare'], 'cash_or_manual_card', 'completed', f'manual_{reservation_id}_{int(time.time())}', datetime.now(), datetime.now()))
    conn.commit()

    log_action(f"Usuario {session.get('user_id', 'System')} procesó pago manual para reserva {reservation_id}", "success")

    # Enviar correo de confirmación
    try:
        html_body = render_template('emails/confirmation_email.html',
                                    reservation=reservation,
                                    flight=reservation, 
                                    customer=reservation,
                                    ticket_url=url_for('boarding_pass', reservation_id=reservation_id, _external=True))
        send_email(subject="Confirmación de Reserva - UMES AIR",
                   sender_email=app.config['MAIL_USERNAME'],
                   recipients=[reservation['email']],
                   html_body=html_body)
        log_action(f"Correo de confirmación enviado a {reservation['email']} para reserva {reservation_id}", "success")
    except Exception as e:
        log_action(f"Error enviando correo de confirmación para reserva {reservation_id}: {e}", "error")

    flash('Pago procesado y reservación confirmada.', 'success')
    flash('Pago procesado con éxito!', 'success')
    
    conn.close()
    return redirect(url_for('view_reservation', reservation_id=reservation_id))

@app.route('/reservations/<reservation_id>/cancel', methods=['POST'])
@login_required
def cancel_reservation(reservation_id):
    conn = get_db_connection()
    
    # Obtener la reservación
    reservation = conn.execute('SELECT * FROM reservations WHERE id = ?', (reservation_id,)).fetchone()
    
    if not reservation:
        conn.close()
        flash('Reservación no encontrada.', 'error')
        return redirect(url_for('dashboard'))
    
    # Verificar si el usuario puede cancelar esta reservación
    if session['user_role'] == 'passenger' and reservation['customer_id'] != session['user_id']:
        conn.close()
        flash('No tienes permiso para cancelar esta reservación.', 'error')
        return redirect(url_for('dashboard'))
    
    # Actualizar status para cancelado
    conn.execute('UPDATE reservations SET status = ? WHERE id = ?', ('cancelled', reservation_id))
    conn.commit()
    
    log_action(f"Usuario {session['user_id']} canceló reserva {reservation_id}", "success")
    flash('Reservación cancelada con éxito!', 'success')

    # Enviar correo de cancelación de reservación
    try:
        customer = conn.execute('SELECT * FROM customers WHERE id = ?', (reservation['customer_id'],)).fetchone()
        flight = conn.execute('SELECT * FROM flights WHERE id = ?', (reservation['flight_id'],)).fetchone()
        html_body = render_template('emails/flight_cancellation_email.html',
                                    customer_name=customer['first_name'],
                                    flight=flight,
                                    cancellation_reason='Su reservación ha sido cancelada a petición suya.',
                                    now=datetime.now()) # O un motivo más genérico si es cancelación administrativa
        send_email(subject=f"Confirmación de Cancelación de Reservación - Vuelo {flight['flight_code']} - UMES AIR",
                   sender_email=app.config['MAIL_DEFAULT_SENDER'],
                   recipients=[customer['email']],
                   html_body=html_body)
        log_action(f"Correo de cancelación de reservación enviado a {customer['email']} para reserva ID {reservation_id}", "success")
    except Exception as e:
        log_action(f"Error enviando correo de cancelación de reservación para reserva ID {reservation_id}: {e}", "error")
    
    conn.close()
    
    if session['user_role'] == 'passenger':
        return redirect(url_for('dashboard'))
    else:
        return redirect(url_for('view_reservation', reservation_id=reservation_id))

@app.route('/reservations/<reservation_id>/boarding-pass')
#@login_required
def boarding_pass(reservation_id):
    conn = get_db_connection()
    
    # Obtener reservación con detalles del vuelo y cliente
    reservation = conn.execute('''
        SELECT r.*, f.flight_code, f.date, f.origin, f.destination, f.departure_time, f.arrival_time,
               c.first_name, c.last_name, c.id_number
        FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        WHERE r.id = ? AND r.status = 'paid'
    ''', (reservation_id,)).fetchone()
    
    if not reservation:
        conn.close()
        flash('Reservación pagada no encontrada.', 'error')
        return redirect(url_for('dashboard'))
    
    # Verificar si el usuario tiene acceso a esta tarjeta de embarque
    if session['user_role'] == 'passenger' and reservation['customer_id'] != session['user_id']:
        conn.close()
        flash('No tienes permiso para ver esta tarjeta de embarque.', 'error')
        return redirect(url_for('dashboard'))
    
    # Verificar si la salida del vuelo es en el futuro
    flight_datetime = datetime.strptime(f"{reservation['date']} {reservation['departure_time']}", '%Y-%m-%d %H:%M')
    if datetime.now() > flight_datetime:
        flash('Este vuelo ya ha partido.', 'warning')

    # Calculate flight duration
    departure_dt = datetime.strptime(reservation['departure_time'], '%H:%M')
    arrival_dt = datetime.strptime(reservation['arrival_time'], '%H:%M')
    duration_delta = arrival_dt - departure_dt
    if duration_delta.total_seconds() < 0: # Handles overnight flights if arrival_dt is on the next day
        duration_delta += timedelta(days=1)
    duration_hours = int(duration_delta.total_seconds() // 3600)
    duration_minutes = int((duration_delta.total_seconds() % 3600) // 60)
    flight_duration_str = f"{duration_hours}h {duration_minutes}m"

    # Prepare data for QR code and template
    reservation_dict = dict(reservation) # Convert sqlite3.Row to dict

    # Check if gate and boarding_time exist, if not, generate and store them
    if not reservation_dict.get('gate') or not reservation_dict.get('boarding_time'):
        possible_gates = [f"{random.choice(['A', 'B', 'C', 'D'])}{random.randint(1, 15)}" for _ in range(5)]
        random_gate = random.choice(possible_gates)
        boarding_time_dt = flight_datetime - timedelta(minutes=45)
        boarding_time_str = boarding_time_dt.strftime('%H:%M')
        
        # Reopen connection if closed, or use existing if still open
        conn_update = get_db_connection() 
        try:
            conn_update.execute('''
                UPDATE reservations
                SET gate = ?, boarding_time = ?
                WHERE id = ?
            ''', (random_gate, boarding_time_str, reservation_id))
            conn_update.commit()
            # Re-fetch reservation to get the updated values
            reservation = conn_update.execute('''
                SELECT r.*, f.flight_code, f.date, f.origin, f.destination, f.departure_time, f.arrival_time,
                       c.first_name, c.last_name, c.id_number
                FROM reservations r
                JOIN flights f ON r.flight_id = f.id
                JOIN customers c ON r.customer_id = c.id
                WHERE r.id = ?
            ''', (reservation_id,)).fetchone()
            reservation_dict = dict(reservation) # Update dict with new values
        finally:
            conn_update.close()

    reservation_dict['flight_duration'] = flight_duration_str
    # Ensure gate and boarding_time are from the potentially updated reservation_dict
    random_gate = reservation_dict.get('gate') # Use .get for safety, though it should exist now
    boarding_time_str = reservation_dict.get('boarding_time') # Use .get for safety

    qr_content = (
        f"RESERVATION_ID:{reservation_dict.get('id', 'N/A')}; "
        f"FLIGHT:{reservation_dict.get('flight_code', 'N/A')}; "
        f"PASSENGER:{reservation_dict.get('first_name', '')} {reservation_dict.get('last_name', '')}; "
        f"DATE:{reservation_dict.get('date', 'N/A')}; "
        f"SEAT:{reservation_dict.get('seat_number', 'N/A')}; " # Ensure seat_number is in reservation_dict
        f"FROM:{reservation_dict.get('origin', 'N/A')}; "
        f"TO:{reservation_dict.get('destination', 'N/A')}"
    )

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=4,
    )
    qr.add_data(qr_content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    qr_code_image_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
    
    conn.close() # Close DB connection as it's no longer needed for this request path
    
    # Render HTML template to string, passing the QR code image and other details
    html_string = render_template(
        'reservations/boarding_pass.html', 
        reservation=reservation_dict, 
        qr_code_image=qr_code_image_base64
    )
    
    if request.args.get('format') == 'pdf':
        # Render the PDF-specific template
        pdf_html_content = render_template(
            'reservations/boarding_pass_pdf.html', 
            reservation=reservation_dict, 
            qr_code_image=qr_code_image_base64
        )
        # Add CSS for landscape orientation and scaling
        css = CSS(string='''
            @page {
                size: landscape;
                margin: 0.25in; /* Adjusted margin */
            }
            body {
                transform: scale(0.65); /* Adjusted scale for better fit */
                transform-origin: top left;
                width: 153.846%; /* 100 / 0.65 */
                /* Ensure content does not overflow due to scaling */
                max-width: 153.846%; 
                overflow: hidden; 
            }
            /* Specific adjustments for boarding pass container if needed */
            .boarding-pass-container {
                /* Add any specific styles to help with layout in PDF */
            }
        ''')
        pdf_bytes = HTML(string=pdf_html_content, base_url=request.base_url).write_pdf(stylesheets=[css])
        response = make_response(pdf_bytes)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename=boarding_pass_{reservation_id}.pdf'
        return response
    
    # Otherwise, return HTML page (for viewing in browser before download)
    # This uses the original boarding_pass.html which includes base.html
    return render_template(
        'reservations/boarding_pass.html', 
        reservation=reservation_dict, 
        qr_code_image=qr_code_image_base64
    )

# ------- Informes (Solo Admin) -------
@app.route('/reports')
@login_required
@role_required(['admin'])
def reports():
    return render_template('reports/index.html')

@app.route('/reports/passengers-by-flight', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def passengers_by_flight():
    conn = get_db_connection()
    
    if request.method == 'POST':
        flight_id = request.form['flight_id']
        
        # Obtener detalles del vuelo
        flight = conn.execute('SELECT * FROM flights WHERE id = ?', (flight_id,)).fetchone()
        
        # Obtener lista de pasajeros
        passengers = conn.execute('''
            SELECT r.seat_number, c.first_name, c.last_name, c.id_number, c.nationality, 
                   c.gender, c.email, c.phone, r.status
            FROM reservations r
            JOIN customers c ON r.customer_id = c.id
            WHERE r.flight_id = ? AND r.status != 'cancelled'
            ORDER BY r.seat_number
        ''', (flight_id,)).fetchall()
        
        # Obtener estadísticas del vuelo
        stats = conn.execute('''
            SELECT 
                COUNT(*) as total_reservations,
                SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) as confirmed_passengers,
                (SELECT capacity FROM flights WHERE id = ?) as total_capacity
            FROM reservations
            WHERE flight_id = ? AND status != 'cancelled'
        ''', (flight_id, flight_id)).fetchone()
        
        conn.close()
        
        log_action(f"Admin generó lista de pasajeros para el vuelo {flight['flight_code']}", "success")
        return render_template('reports/passengers_by_flight.html', 
                             flight=flight, 
                             passengers=passengers,
                             stats=stats,
                             generated=True)
    
    # Obtener todos los vuelos para el desplegable
    flights = conn.execute('''
        SELECT id, flight_code, date, origin, destination
        FROM flights
        ORDER BY date DESC, departure_time
    ''').fetchall()
    
    conn.close()
    
    return render_template('reports/passengers_by_flight.html', 
                         flights=flights,
                         generated=False)

@app.route('/reports/occupancy', methods=['GET', 'POST'])
@login_required
@role_required(['admin'])
def occupancy_report():
    conn = get_db_connection()
    
    if request.method == 'POST':
        start_date = request.form['start_date']
        end_date = request.form['end_date']
        
        # Obtener datos de ocupación
        occupancy = conn.execute('''
            SELECT f.id, f.flight_code, f.date, f.origin, f.destination, f.capacity,
                   COUNT(r.id) as booked,
                   ROUND((COUNT(r.id) * 100.0 / f.capacity), 2) as occupancy_rate
            FROM flights f
            LEFT JOIN reservations r ON f.id = r.flight_id AND r.status != 'cancelled'
            WHERE f.date BETWEEN ? AND ?
            GROUP BY f.id
            ORDER BY f.date, f.departure_time
        ''', (start_date, end_date)).fetchall()
        
        # Calcular estadísticas resumidas
        if occupancy:
            total_capacity = sum(flight['capacity'] for flight in occupancy)
            total_booked = sum(flight['booked'] for flight in occupancy)
            average_occupancy = round((total_booked * 100.0 / total_capacity), 2) if total_capacity > 0 else 0
        else:
            total_capacity = 0
            total_booked = 0
            average_occupancy = 0
        
        conn.close()
        
        log_action(f"Admin generó informe de ocupación desde {start_date} hasta {end_date}", "success")
        return render_template('reports/occupancy.html', 
                             occupancy=occupancy,
                             start_date=start_date,
                             end_date=end_date,
                             total_capacity=total_capacity,
                             total_booked=total_booked,
                             average_occupancy=average_occupancy,
                             generated=True)
    
    conn.close()
    
    # El rango de fechas predeterminado es el mes actual
    today = datetime.now()
    start_date = datetime(today.year, today.month, 1).strftime('%Y-%m-%d')
    end_date = datetime(today.year, today.month + 1, 1) if today.month < 12 else datetime(today.year + 1, 1, 1)
    end_date = (end_date - timedelta(days=1)).strftime('%Y-%m-%d')
    
    return render_template('reports/occupancy.html', 
                         start_date=start_date,
                         end_date=end_date,
                         generated=False)

@app.route('/reports/audit-log')
@login_required
@role_required(['admin'])
def audit_log():
    conn = get_db_connection()
    
    # Obtener número de entradas de registro para mostrar (predeterminado 100)
    limit = request.args.get('limit', 100, type=int)
    
    logs = conn.execute('''
        SELECT * FROM audit_log
        ORDER BY timestamp DESC
        LIMIT ?
    ''', (limit,)).fetchall()
    
    conn.close()
    
    return render_template('reports/audit_log.html', logs=logs, limit=limit)

# ------- Sistema de Pagos Fake Gateway -------



def simulate_payment_processing(card_number, amount, reservation_id):
    """Simula el procesamiento de un pago - acepta cualquier número de tarjeta"""
    # Limpiar el número de tarjeta
    clean_card = card_number.replace(' ', '').replace('-', '')
    
    # Validación básica del número de tarjeta (debe tener entre 13-19 dígitos)
    if not clean_card.isdigit() or len(clean_card) < 13 or len(clean_card) > 19:
        return {
            'transaction_id': f"TXN_ERROR_{int(time.time())}",
            'status': 'failed',
            'error_message': 'Número de tarjeta debe tener entre 13-19 dígitos',
            'processing_time': 1.0,
            'card_type': 'Inválida',
            'bank_name': 'N/A'
        }
    
    # Simular tiempo de procesamiento (1.5 a 4 segundos)
    processing_time = random.uniform(1.5, 4.0)
    time.sleep(processing_time)
    
    # Determinar tipo de tarjeta basado en el primer dígito
    first_digit = clean_card[0]
    if first_digit == '4':
        card_type = 'Visa'
        bank_name = 'Banco Visa'
    elif first_digit == '5':
        card_type = 'MasterCard'
        bank_name = 'Banco MasterCard'
    elif first_digit == '3':
        card_type = 'American Express'
        bank_name = 'Banco AmEx'
    else:
        card_type = 'Genérica'
        bank_name = 'Banco Genérico'
    
    # Generar ID de transacción
    transaction_id = f"TXN_{uuid.uuid4().hex[:12].upper()}"
    
    # Simular éxito en el 90% de los casos para cualquier tarjeta válida
    success_rate = 0.90
    if random.random() < success_rate:
        status = 'completed'
        error_message = None
    else:
        # Simular diferentes tipos de errores ocasionales
        errors = [
            'Fondos insuficientes',
            'Tarjeta declinada por el banco',
            'Error de procesamiento temporal',
            'Tiempo de espera agotado'
        ]
        status = 'failed'
        error_message = random.choice(errors)
    
    # Registrar transacción en la base de datos
    conn = get_db_connection()
    # Determinar payment_method basado en card_type
    payment_method = 'card' # Valor genérico, se puede refinar si es necesario
    if card_type == 'Visa':
        payment_method = 'visa_card'
    elif card_type == 'MasterCard':
        payment_method = 'mastercard_card'
    elif card_type == 'American Express':
        payment_method = 'amex_card'

    conn.execute('''
        INSERT INTO payments 
        (id, reservation_id, amount, currency, payment_method, status, 
         card_last_four, card_type, bank_name, error_message, processing_time_seconds, transaction_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        transaction_id, reservation_id, amount, 'USD', payment_method, status,
        clean_card[-4:], card_type, bank_name, error_message,
        round(processing_time, 2), datetime.now()
    ))
    conn.commit()
    conn.close()
    
    return {
        'transaction_id': transaction_id,
        'status': status,
        'error_message': error_message,
        'processing_time': processing_time,
        'card_type': card_type,
        'bank_name': bank_name
    }

def send_payment_notification(email, reservation_data, transaction_data):
    """Envía notificación de confirmación de pago por email."""
    try:
        subject = f"Confirmación de Pago - Reserva Umes Air #{reservation_data['id']}"
        # Renderizar el cuerpo del email desde una plantilla HTML
        # Usar la plantilla 'emails/confirmation_email.html'
        html_body = render_template(
            'emails/confirmation_email.html',
            customer_name=f"{reservation_data['first_name'] if reservation_data['first_name'] else ''} {reservation_data['last_name'] if reservation_data['last_name'] else ''}",
            flight=reservation_data, # Pasar reservation_data como 'flight', ya que contiene flight_number, origin, etc.
            reservation=reservation_data, # Pasar reservation_data también como 'reservation' para id, seat_number, price, status
            now=datetime.now(timezone.utc), # Para el año en el footer y para evitar DeprecationWarning
            transaction=transaction_data # Se mantiene por si es necesario en el futuro o en alguna inclusión
        )
        
        msg = Message(subject, recipients=[email], html=html_body)
        mail.send(msg)
        log_action(f"Email de confirmación de pago enviado a {email} para transacción {transaction_data['transaction_id']}", "success")
        return True
    except Exception as e:
        log_action(f"Error al enviar email de confirmación a {email}: {str(e)}", "error")
        # Considera registrar el error de forma más detallada o notificar a administradores
        print(f"Error sending email: {e}") # Para depuración en consola
        return False

@app.route('/payment/process/<reservation_id>', methods=['POST'])
@login_required
@role_required(['admin', 'agent'])
def process_payment(reservation_id):
    """Procesa el pago usando el simulador"""
    conn = get_db_connection()
    
    # Obtener datos de la reservación
    reservation = conn.execute('''
        SELECT r.*, f.*, c.*,
               f.id as flight_id, f.flight_code, f.date, f.origin, f.destination,
               f.departure_time, f.arrival_time, f.base_fare, f.total_fare,
               c.id as customer_id, c.first_name, c.last_name, c.email
        FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        WHERE r.id = ? AND r.status = 'pending'
    ''', (reservation_id,)).fetchone()
    
    if not reservation:
        conn.close()
        return jsonify({'success': False, 'error': 'Reservación no encontrada o ya procesada'})
    
    # Obtener datos del formulario
    card_number = request.form.get('card_number', '').replace(' ', '')
    card_holder = request.form.get('card_name', '')  # Corregido: era card_holder
    expiry_date = request.form.get('expiry', '')     # Corregido: era expiry_date
    cvv = request.form.get('cvv', '')
    
    # Validaciones básicas
    if not all([card_number, card_holder, expiry_date, cvv]):
        print(f"DEBUG - Campos recibidos: card_number={card_number}, card_holder={card_holder}, expiry_date={expiry_date}, cvv={cvv}")
        conn.close()
        return jsonify({'success': False, 'error': 'Todos los campos son requeridos'})
    
    if len(card_number) < 13 or len(card_number) > 19:
        conn.close()
        return jsonify({'success': False, 'error': 'Número de tarjeta inválido'})
    
    # Simular procesamiento de pago
    try:
        payment_result = simulate_payment_processing(
            card_number, 
            float(reservation['total_fare']), 
            reservation_id
        )
        
        if payment_result['status'] == 'completed':
            # Actualizar reservación como pagada
            conn.execute(
                'UPDATE reservations SET status = ?, payment_date = ? WHERE id = ?',
                ('paid', datetime.now(), reservation_id)
            )
            conn.commit()
            
            # Enviar notificación por email (simulado)
            send_payment_notification(
                reservation['email'],
                reservation,
                payment_result
            )
            
            log_action(f"Pago procesado exitosamente para reserva {reservation_id}", "success")
            
            conn.close()
            return jsonify({
                'success': True,
                'transaction_id': payment_result['transaction_id'],
                'redirect_url': url_for('payment_transaction_success', transaction_id=payment_result['transaction_id'])
            })
        else:
            log_action(f"Pago fallido para reserva {reservation_id}: {payment_result['error_message']}", "error")
            
            conn.close()
            return jsonify({
                'success': False,
                'transaction_id': payment_result['transaction_id'],
                'error': payment_result['error_message'],
                'redirect_url': url_for('payment_failed', transaction_id=payment_result['transaction_id'])
            })
            
    except Exception as e:
        if conn: # Asegurarse de que conn existe antes de intentar cerrarla
            conn.close()
        error_message = f"Error procesando pago para reserva {reservation_id}: {str(e)}"
        log_action(error_message, "error")
        # Imprimir el error en la consola del servidor para más detalles
        print(f"DEBUG: {error_message}") 
        import traceback
        traceback.print_exc() # Imprime el traceback completo
        return jsonify({'success': False, 'error': str(e)}) # Devolver el mensaje de error específico

@app.route('/payment/transaction/success/<transaction_id>')
@login_required
@role_required(['admin', 'agent']) # Permitir acceso a clientes que acaban de pagar
def payment_transaction_success(transaction_id):
    """Página de éxito del pago, accesible por el cliente después de pagar."""
    conn = get_db_connection()
    
    payment = conn.execute('SELECT * FROM payments WHERE id = ? AND status = ?', (transaction_id, 'completed')).fetchone()
    
    if not payment:
        conn.close()
        flash('Transacción de pago no encontrada o no completada.', 'error')
        return redirect(url_for('dashboard'))

    # Obtener datos de la reservación asociados al pago
    reservation = conn.execute('''
        SELECT r.*, f.flight_code, f.date AS flight_date, f.origin, f.destination, 
               f.departure_time, f.arrival_time, f.total_fare AS flight_total_fare,
               c.first_name, c.last_name, c.email AS customer_email
        FROM reservations r
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        WHERE r.id = ?
    ''', (payment['reservation_id'],)).fetchone()
    
    if not reservation:
        # Esto no debería ocurrir si el pago existe y tiene un reservation_id válido
        conn.close()
        flash('Reservación asociada a la transacción no encontrada.', 'error')
        return redirect(url_for('dashboard'))
    
    conn.close()
    return render_template('reservations/success.html', reservation=reservation, payment=payment)

@app.route('/payment/failed/<transaction_id>')
@login_required
@role_required(['admin', 'agent'])
def payment_failed(transaction_id):
    """Página de fallo del pago"""
    conn = get_db_connection()
    
    # Obtener datos de la transacción y reservación
    transaction = conn.execute('''
        SELECT pt.*, r.*, f.*, c.*,
               f.flight_code, f.date, f.origin, f.destination,
               f.departure_time, f.arrival_time,
               c.first_name, c.last_name, c.email
        FROM payments pt
        JOIN reservations r ON pt.reservation_id = r.id
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        WHERE pt.id = ? AND pt.status = 'failed'
    ''', (transaction_id,)).fetchone()
    
    if not transaction:
        conn.close()
        flash('Transacción no encontrada.', 'error')
        return redirect(url_for('dashboard'))
    
    conn.close()
    return render_template('payments/failed.html', transaction=transaction)

@app.route('/payment/history')
@login_required
@role_required(['admin', 'agent'])
def payment_history():
    """Historial de transacciones de pago"""
    conn = get_db_connection()
    
    # Obtener estadísticas
    stats = conn.execute('''
        SELECT 
            COUNT(*) as total_transactions,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as successful_transactions,
            ROUND(AVG(CASE WHEN status = 'completed' THEN 1.0 ELSE 0.0 END) * 100, 2) as success_rate,
            SUM(CASE WHEN status = 'completed' THEN amount ELSE 0 END) as total_amount
        FROM payments
    ''').fetchone()
    
    # Distribución por tipo de tarjeta
    card_distribution = conn.execute('''
        SELECT card_type, COUNT(*) as count
        FROM payments
        WHERE status = 'completed'
        GROUP BY card_type
        ORDER BY count DESC
    ''').fetchall()
    
    # Distribución por banco
    bank_distribution = conn.execute('''
        SELECT bank_name, COUNT(*) as count
        FROM payments
        WHERE status = 'completed'
        GROUP BY bank_name
        ORDER BY count DESC
        LIMIT 5
    ''').fetchall()
    
    # Transacciones recientes
    recent_transactions = conn.execute('''
        SELECT pt.*, r.id as reservation_id, f.flight_code,
               c.first_name, c.last_name
        FROM payments pt
        JOIN reservations r ON pt.reservation_id = r.id
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        ORDER BY pt.created_at DESC
        LIMIT 20
    ''').fetchall()
    
    conn.close()
    
    return render_template('payments/history.html',
                         stats=stats,
                         card_distribution=card_distribution,
                         bank_distribution=bank_distribution,
                         recent_transactions=recent_transactions)

@app.route('/payment/transaction/<transaction_id>')
@login_required
@role_required(['admin', 'agent'])
def transaction_detail(transaction_id):
    """Detalle de una transacción específica"""
    conn = get_db_connection()
    
    transaction = conn.execute('''
        SELECT pt.*, r.*, f.*, c.*,
               f.flight_code, f.date, f.origin, f.destination,
               f.departure_time, f.arrival_time,
               c.first_name, c.last_name, c.email, c.phone
        FROM payments pt
        JOIN reservations r ON pt.reservation_id = r.id
        JOIN flights f ON r.flight_id = f.id
        JOIN customers c ON r.customer_id = c.id
        WHERE pt.id = ?
    ''', (transaction_id,)).fetchone()
    
    if not transaction:
        conn.close()
        flash('Transacción no encontrada.', 'error')
        return redirect(url_for('payment_history'))
    
    conn.close()
    return render_template('payments/transaction_detail.html', transaction=transaction)

@app.route('/payment/test-cards')
@login_required
@role_required(['admin', 'agent'])


# ------- Funciones Auxiliares -------
def log_action(action, result, conn=None):
    manage_conn = False
    if conn is None:
        conn = get_db_connection()
        manage_conn = True

    user_id = session.get('user_id', 'anonymous')
    try:
        conn.execute(
            'INSERT INTO audit_log (user_id, action, result, timestamp) VALUES (?, ?, ?, ?)',
            (user_id, action, result, datetime.now())
        )
        if manage_conn:
            conn.commit()
    finally:
        if manage_conn:
            conn.close()

# ------- Aplicación Principal -------

# Inicialización automática de la base de datos
import os

# Verificar si el archivo de base de datos existe y tiene contenido
db_path = app.config['DATABASE']
# Asegurarse de que el tamaño solo se comprueba si el archivo existe para evitar errores
db_exists_check = os.path.exists(db_path)
db_has_content = db_exists_check and os.path.getsize(db_path) > 0
db_should_initialize = not (db_exists_check and db_has_content)

print(f"DEBUG: Verificando base de datos. Path: {db_path}, Existe: {db_exists_check}, Tiene contenido: {db_has_content}, Debe inicializar: {db_should_initialize}")

if db_should_initialize:
    print("INFO: Iniciando la creación de la base de datos...")
    try:
        # Si el archivo existe pero está vacío, eliminarlo para asegurar una creación limpia.
        if db_exists_check and not db_has_content:
            print("DEBUG: La base de datos existe pero está vacía. Eliminándola para recreación.")
            os.remove(db_path)
            print(f"DEBUG: Archivo {db_path} eliminado.")
        
        with app.app_context():
            print("DEBUG: Dentro del contexto de la aplicación para inicializar DB.")
            conn = get_db_connection()
            print(f"DEBUG: Conexión a la base de datos obtenida: {conn}")
            # Asegurarse de que el archivo schema.sql se abre en modo lectura 'r'
            with open('schema.sql', 'r') as f_schema:
                schema_content = f_schema.read()
                print("DEBUG: Contenido de schema.sql leído.")
                conn.executescript(schema_content)
                print("DEBUG: schema.sql ejecutado.")
            
            print("DEBUG: Intentando añadir usuario administrador por defecto...")
            password_hash = hashlib.sha256('admin123'.encode()).hexdigest()
            conn.execute('INSERT OR IGNORE INTO users (id, name, email, password, role, status) VALUES (?, ?, ?, ?, ?, ?)',
                        ('admin1', 'Administrator', 'admin@umesair.com', password_hash, 'admin', 'active'))
            print("DEBUG: Usuario administrador insertado (o ignorado si ya existía).")
            conn.commit()
            print("DEBUG: Commit realizado en la base de datos.")
            conn.close()
            print("INFO: Base de datos inicializada y configurada correctamente.")
    except sqlite3.Error as e_sqlite:
        print(f"ERROR SQLITE DURANTE LA INICIALIZACIÓN DE LA BASE DE DATOS: {str(e_sqlite)}")
        print(f"Detalles del error SQLite: {e_sqlite.args}")
    except Exception as e_general:
        print(f"ERROR GENERAL DURANTE LA INICIALIZACIÓN DE LA BASE DE DATOS: {str(e_general)}")
else:
    print("INFO: La base de datos ya existe y tiene contenido, omitiendo inicialización.")

import re # For regex validation
from datetime import datetime # For date validation

@app.route('/request-reservation-email', methods=['POST'])
def request_reservation_email():
    form_data = request.form.to_dict()
    errors = {}

    # --- VALIDATIONS START ---
    required_fields_not_logged_in = [
        'flight_id', 'first_name', 'last_name', 'email', 'phone',
        'nationality', 'date_of_birth', 'gender', 'id_number', 'emergency_contact'
    ]
    required_fields_logged_in = ['flight_id']

    is_logged_in_passenger = 'user_id' in session and session.get('user_role') == 'passenger'

    current_required_fields = required_fields_logged_in if is_logged_in_passenger else required_fields_not_logged_in

    for field in current_required_fields:
        if not form_data.get(field):
            errors[field] = f"El campo '{field.replace('_', ' ').capitalize()}' es obligatorio."

    # Specific validations for non-logged-in users or if fields are present
    if not is_logged_in_passenger:
        email = form_data.get('email')
        if email and not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
            errors['email'] = "El formato del correo electrónico no es válido."

        phone = form_data.get('phone')
        if phone and not re.match(r"^\+?[0-9\s-]{7,15}$", phone):
            errors['phone'] = "El formato del teléfono no es válido."

        date_of_birth = form_data.get('date_of_birth')
        if date_of_birth:
            try:
                datetime.strptime(date_of_birth, '%Y-%m-%d')
            except ValueError:
                errors['date_of_birth'] = "El formato de la fecha de nacimiento no es válido (YYYY-MM-DD)."
        
        id_number = form_data.get('id_number')
        if id_number and len(id_number) < 5:
             errors['id_number'] = "El número de identificación parece demasiado corto."

    # Credit card validations (if any card field is filled, all become 'conditionally required' and validated)
    card_holder_name = form_data.get('card_holder_name')
    card_number = form_data.get('card_number')
    card_expiry = form_data.get('card_expiry')
    card_cvc = form_data.get('card_cvc')

    # Normalize card number by removing spaces and non-digits for validation
    cleaned_card_number = re.sub(r'[^0-9]', '', card_number) if card_number else ''

    if card_holder_name or card_number or card_expiry or card_cvc:
        if not card_holder_name:
            errors['card_holder_name'] = "El nombre del titular de la tarjeta es obligatorio si proporciona datos de tarjeta."
        if not card_number:
            errors['card_number'] = "El número de tarjeta es obligatorio si proporciona datos de tarjeta."
        elif not re.match(r"^[0-9]{13,19}$", cleaned_card_number):
            errors['card_number'] = "El número de tarjeta debe tener entre 13 y 19 dígitos."
        
        if not card_expiry:
            errors['card_expiry'] = "La fecha de expiración es obligatoria si proporciona datos de tarjeta."
        elif not re.match(r"^(0[1-9]|1[0-2])\/([0-9]{2})$", card_expiry):
            errors['card_expiry'] = "El formato de la fecha de expiración debe ser MM/AA."
        else:
            try:
                exp_month, exp_year_short = map(int, card_expiry.split('/'))
                current_year_short = datetime.now().year % 100
                current_month = datetime.now().month
                # Assuming year is 20xx
                if exp_year_short < current_year_short or (exp_year_short == current_year_short and exp_month < current_month):
                    errors['card_expiry'] = "La tarjeta ha expirado."
            except ValueError:
                 errors['card_expiry'] = "Fecha de expiración inválida."

        if not card_cvc:
            errors['card_cvc'] = "El CVC/CVV es obligatorio si proporciona datos de tarjeta."
        elif not re.match(r"^[0-9]{3,4}$", card_cvc):
            errors['card_cvc'] = "El CVC/CVV debe tener 3 o 4 dígitos."

    if errors:
        for field, message in errors.items():
            flash(message, 'danger')
        # To repopulate form, ideally pass form_data back to template or store in session
        # For now, just redirecting. User will have to re-enter.
        return redirect(url_for('index'))
    # --- VALIDATIONS END ---

    # CSRF Protection (simple check) - Descomentar si se implementa un token más robusto
    # form_csrf_token = request.form.get('csrf_token')
    # session_csrf_token = session.pop('_csrf_token', None) # Pop to ensure one-time use
    # if not form_csrf_token or form_csrf_token != session_csrf_token:
    #     flash('Solicitud no válida o token CSRF caducado. Inténtelo de nuevo.', 'danger')
    #     return redirect(url_for('index'))

    conn = get_db_connection()
    # form_data already defined and validated
    user_details = {}

    if 'user_id' in session and session.get('user_role') == 'passenger':
        is_logged_in_passenger = True
        # Intentar obtener datos del cliente si existen
        user_query = conn.execute('SELECT u.name, u.email, c.first_name, c.last_name, c.phone, c.nationality, c.date_of_birth, c.gender, c.id_number, c.emergency_contact FROM users u LEFT JOIN customers c ON u.email = c.email WHERE u.id = ?', (session['user_id'],)).fetchone()
        if user_query:
            user_details['name'] = user_query['name'] # Nombre del usuario de la tabla users
            user_details['email'] = user_query['email'] # Email del usuario de la tabla users
            # Datos del cliente si existen en la tabla customers
            if user_query['first_name']:
                user_details['first_name'] = user_query['first_name']
                user_details['last_name'] = user_query['last_name']
                user_details['phone'] = user_query['phone']
                user_details['nationality'] = user_query['nationality']
                user_details['date_of_birth'] = user_query['date_of_birth']
                user_details['gender'] = user_query['gender']
                user_details['id_number'] = user_query['id_number']
                user_details['emergency_contact'] = user_query['emergency_contact']
            else: # Si no hay datos de cliente, usar los del usuario para nombre y email
                user_details['first_name'] = user_query['name'].split(' ')[0] if user_query['name'] else ''
                user_details['last_name'] = ' '.join(user_query['name'].split(' ')[1:]) if user_query['name'] and ' ' in user_query['name'] else ''
        
        # Actualizar form_data con los detalles del usuario logueado, 
        # pero permitir que los campos del formulario (si el usuario los llena) tengan precedencia para datos de tarjeta, etc.
        for key, value in user_details.items():
            if key not in form_data or not form_data[key]: # Solo si el campo no vino del form o vino vacío
                 form_data[key] = value
    
    selected_flight_id = form_data.get('flight_id')
    flight_info = None
    if selected_flight_id:
        flight_info = conn.execute('SELECT flight_code, date, origin, destination, departure_time FROM flights WHERE id = ?', (selected_flight_id,)).fetchone()
    
    conn.close()

    email_subject = "Nueva Solicitud de Reserva de Vuelo"
    recipient_email = app.config.get('MAIL_USERNAME') 

    if not recipient_email:
        flash('Error de configuración: No se ha definido un correo para recibir solicitudes.', 'danger')
        log_action("Error: MAIL_USERNAME no configurado para solicitud de reserva.", "error")
        return redirect(url_for('index'))

    try:
        # Asegurarse que el email del solicitante para el admin sea el correcto
        requester_email_for_admin = user_details.get('email') if is_logged_in_passenger else form_data.get('email')
        requester_name_for_admin = user_details.get('name') if is_logged_in_passenger else f"{form_data.get('first_name','')} {form_data.get('last_name','')}".strip()
        if not requester_name_for_admin and requester_email_for_admin: # Fallback si el nombre está vacío
             requester_name_for_admin = requester_email_for_admin

        email_sent_to_admin = send_email(
            subject=email_subject,
            recipients=[recipient_email],
            template='reservation_request_admin_email.html', 
            form_data=form_data,
            flight_info=flight_info,
            is_logged_in_passenger=is_logged_in_passenger,
            requester_name=requester_name_for_admin,
            requester_email=requester_email_for_admin
        )

        if email_sent_to_admin:
            flash('Su solicitud de reserva ha sido enviada. Nos pondremos en contacto con usted pronto.', 'success')
            log_action(f"Solicitud de reserva enviada por {requester_email_for_admin} para vuelo ID {selected_flight_id}", "info")
            
            # Enviar confirmación al usuario que solicitó
            customer_email_address = user_details.get('email') if is_logged_in_passenger else form_data.get('email')
            if customer_email_address:
                send_email(
                    subject="Hemos Recibido tu Solicitud de Reserva - UMES Air",
                    recipients=[customer_email_address],
                    template='reservation_request_customer_email.html', 
                    form_data=form_data,
                    flight_info=flight_info
                )
        else:
            flash('Hubo un problema al enviar su solicitud al administrador. Por favor, inténtelo más tarde.', 'danger')
    except Exception as e:
        flash('Ocurrió un error procesando su solicitud. Por favor, contacte a soporte.', 'danger')
        log_action(f"Error procesando solicitud de reserva: {str(e)}", "error")
        app.logger.error(f"Error en request_reservation_email: {str(e)}")

    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)