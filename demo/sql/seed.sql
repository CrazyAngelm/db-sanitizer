-- Synthetic-only demo data. Never use this file as a source of production PII.
WITH generated_customers AS (
    SELECT
        customer_number,
        CASE
            WHEN customer_number = 53 THEN NULL
            ELSE (ARRAY[
                'Алексей', 'Мария', 'Дмитрий', 'Анна', 'Сергей',
                'Елена', 'Илья', 'Ольга', 'Максим', 'Наталья'
            ])[((customer_number - 1) % 10) + 1]
            || ' ' ||
            (ARRAY[
                'Соколов', 'Кузнецов', 'Орлов', 'Волков', 'Лебедев',
                'Морозов', 'Петров', 'Васильев', 'Новиков', 'Фёдоров'
            ])[((customer_number - 1) / 10) + 1]
        END AS full_name,
        CASE
            WHEN customer_number = 54 THEN NULL
            ELSE format('client%s@source.demo', lpad(customer_number::text, 3, '0'))
        END AS email,
        CASE
            WHEN customer_number = 55 THEN NULL
            WHEN customer_number % 3 = 0 THEN format('+7 999 100-00-%s', lpad(customer_number::text, 2, '0'))
            WHEN customer_number % 3 = 1 THEN format('8 (999) 200-00-%s', lpad(customer_number::text, 2, '0'))
            ELSE format('799930000%s', lpad(customer_number::text, 2, '0'))
        END AS phone,
        CASE
            WHEN customer_number = 56 THEN NULL
            ELSE format(
                'г. Москва, ул. Тестовая, д. %s, кв. %s',
                customer_number,
                customer_number + 100
            )
        END AS address
    FROM generate_series(1, 60) AS customer_number
)
INSERT INTO public.customers (id, full_name, email, phone, address)
SELECT customer_number, full_name, email, phone, address
FROM generated_customers;

INSERT INTO public.orders (id, customer_id, billing_name, contact_email, amount, created_at)
SELECT
    ((customers.id - 1) * 3) + order_number,
    customers.id,
    customers.full_name,
    customers.email,
    (100 + customers.id * order_number)::numeric(12, 2),
    TIMESTAMPTZ '2026-01-01 00:00:00+00' + (customers.id * order_number) * INTERVAL '1 hour'
FROM public.customers
CROSS JOIN generate_series(1, 3) AS order_number;

INSERT INTO public.support_tickets (
    id,
    customer_id,
    callback_phone,
    delivery_address,
    subject,
    created_at
)
SELECT
    ((customers.id - 1) * 2) + ticket_number,
    customers.id,
    customers.phone,
    customers.address,
    format('Synthetic support ticket %s-%s', customers.id, ticket_number),
    TIMESTAMPTZ '2026-02-01 00:00:00+00' + (customers.id * ticket_number) * INTERVAL '1 hour'
FROM public.customers
CROSS JOIN generate_series(1, 2) AS ticket_number;

SELECT setval(pg_get_serial_sequence('public.customers', 'id'), 60, true);
SELECT setval(pg_get_serial_sequence('public.orders', 'id'), 180, true);
SELECT setval(pg_get_serial_sequence('public.support_tickets', 'id'), 120, true);
