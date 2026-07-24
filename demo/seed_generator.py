"""Create a synthetic PostgreSQL performance fixture with a bounded PII cardinality.

This utility is only for the local demo source database.  It never runs as part
of a normal sanitization job and does not read production data.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True, slots=True)
class SeedCounts:
    customers: int
    orders: int
    tickets: int

    @property
    def total_rows(self) -> int:
        return self.customers + self.orders + self.tickets


def seed_performance_fixture(dsn: str, rows: int, distinct_customers: int = 100) -> SeedCounts:
    """Replace demo data with at least ``rows`` synthetic rows and bounded unique PII."""

    if rows < 100_000:
        raise ValueError("performance fixture requires at least 100000 rows")
    if not 2 <= distinct_customers <= 1_000:
        raise ValueError("distinct_customers must be between 2 and 1000")
    order_rows = rows
    ticket_rows = rows // 2
    with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "TRUNCATE public.support_tickets, public.orders, public.customers "
            "RESTART IDENTITY CASCADE"
        )
        cursor.execute(
            """
            INSERT INTO public.customers (full_name, email, phone, address)
            SELECT
                'Синтетический Клиент ' || customer_number,
                'perf' || lpad(customer_number::text, 4, '0') || '@source.demo',
                '+7 999 500-' || lpad((customer_number / 100)::text, 2, '0')
                    || '-' || lpad(mod(customer_number, 100)::text, 2, '0'),
                '\\u0433. Москва, пр. Производительный, д. ' || customer_number
            FROM generate_series(1, %s) AS customer_number
            """,
            (distinct_customers,),
        )
        cursor.execute(
            """
            INSERT INTO public.orders (customer_id, billing_name, contact_email, amount, created_at)
            SELECT
                ((row_number - 1) %% %s) + 1,
                customers.full_name,
                customers.email,
                (row_number %% 1000)::numeric(12, 2) + 1,
                TIMESTAMPTZ '2026-03-01 00:00:00+00' + row_number * INTERVAL '1 second'
            FROM generate_series(1, %s) AS row_number
            JOIN public.customers ON customers.id = ((row_number - 1) %% %s) + 1
            """,
            (distinct_customers, order_rows, distinct_customers),
        )
        cursor.execute(
            """
            INSERT INTO public.support_tickets (
                customer_id, callback_phone, delivery_address, subject, created_at
            )
            SELECT
                ((row_number - 1) %% %s) + 1,
                customers.phone,
                customers.address,
                format('Synthetic perf ticket %%s', row_number),
                TIMESTAMPTZ '2026-03-01 00:00:00+00' + row_number * INTERVAL '1 second'
            FROM generate_series(1, %s) AS row_number
            JOIN public.customers ON customers.id = ((row_number - 1) %% %s) + 1
            """,
            (distinct_customers, ticket_rows, distinct_customers),
        )
    return SeedCounts(customers=distinct_customers, orders=order_rows, tickets=ticket_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed DB Sanitizer synthetic performance data")
    parser.add_argument("--rows", required=True, type=int)
    parser.add_argument("--distinct-customers", type=int, default=100)
    parser.add_argument("--dsn-env", default="SOURCE_DATABASE_URL")
    args = parser.parse_args()
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"required environment variable {args.dsn_env} is not set")
    counts = seed_performance_fixture(dsn, args.rows, args.distinct_customers)
    print(
        f"seeded synthetic perf fixture: customers={counts.customers} "
        f"orders={counts.orders} tickets={counts.tickets} total_rows={counts.total_rows}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
