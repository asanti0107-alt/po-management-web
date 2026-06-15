# Deploy Notes

## Current Admin Accounts

- `adminit` / `admin123`
- `saksorn@rdthailand.com` / existing password

New sign ups are inactive by default. Admin must assign a role and set Active before the user can sign in.

## Database Reset

The current database backup is:

`Backup Data/database_backup_20260615.db`

To start with an empty production database, do not upload `database.db`. The app creates the schema automatically on first start and seeds the two admin accounts above.

## Required Runtime

Install dependencies from:

`requirements.txt`

Start command:

`uvicorn main:app --host 0.0.0.0 --port $PORT`

For local Windows testing, use a concrete port:

`uvicorn main:app --host 0.0.0.0 --port 8000`

## Cloud Note

GitHub stores the source code, but it does not by itself provide a FastAPI app URL. After pushing this project to GitHub, connect the repository to a cloud host such as Render, Railway, Azure App Service, AWS, or a VM. Use a persistent disk or PostgreSQL if production data must survive redeploys.
