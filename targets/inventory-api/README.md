# Inventory API

This is a small FastAPI inventory service designed as a local chaos-engineering
target. It uses Python 3.11+, SQLAlchemy 2, SQLite, JWT bearer authentication,
and scrypt password hashing.

## Installation

```bash
cd inventory-api
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
cp .env.example .env
```

Set `JWT_SECRET_KEY` in `.env` to a long random value. The default SQLite
database is `inventory.db`; set `DATABASE_URL` to change it.

## Administrator setup

Do not register an administrator through a public endpoint. Set
`ADMIN_EMAIL` and `ADMIN_PASSWORD` in `.env` before the first application
start. The application creates that account once, if the email is not already
present. Use a unique password and keep `.env` out of source control.

## Run

```bash
uvicorn inventory_api.main:app --app-dir src --reload
```

The health check is `GET /health`. Application endpoints use `/api/v1`.

## Example API flow

Register a normal user:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","password":"correct-horse-battery"}'
```

Log in and use the returned bearer token:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"your-admin-password"}'

curl http://127.0.0.1:8000/api/v1/products \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

Administrators can create products and adjust stock:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/products \
  -H 'Authorization: Bearer YOUR_TOKEN' -H 'Content-Type: application/json' \
  -d '{"sku":"W-100","name":"Widget","price":"12.50","quantity":10}'

curl -X POST http://127.0.0.1:8000/api/v1/products/1/inventory/remove \
  -H 'Authorization: Bearer YOUR_TOKEN' -H 'Content-Type: application/json' \
  -d '{"amount":2,"reason":"customer order"}'
```

## Tests

The baseline suite covers intended behavior and avoids the four documented
development defects:

```bash
pytest tests/test_auth.py tests/test_products.py tests/test_inventory.py
```

The strict expected-failure regression suite demonstrates each intentional
defect:

```bash
pytest tests/known_defects
```

GremlinAPI should be given a read-only copy of `src/`, the public API
documentation, and its own writable test directory. Exclude
`CHAOS_TARGETS.md` and `tests/known_defects/` from its initial workspace.

