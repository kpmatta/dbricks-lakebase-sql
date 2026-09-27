# Lakebase SQL Workbench

A Python-first Databricks App for browsing Lakebase Postgres and running SQL as the signed-in Databricks user.

The explorer lists connectable databases, schemas, tables, views, materialized views, foreign and partitioned tables, indexes, sequences, functions, procedures, aggregates, triggers, and user-defined types. Selecting an object loads its definition into the editor and shows its columns, indexes, and constraints. SQL statements can create or update objects; PostgreSQL grants are the authorization boundary. Results are capped at 500 rows, and statements have a 30-second timeout.

## User authorization

Enable Databricks Apps user authorization for the app. Add the `postgres` scope for Lakebase operations. The app also calls `current_user.me()`; the default `iam.current-user:read` scope supports that lookup. A workspace administrator may need to enable user authorization or allow these scopes first. Restrict app `CAN USE` access to intended users.

Each request uses `x-forwarded-access-token` to create a user-scoped `WorkspaceClient`, reads that user's Databricks `user_name`, and mints a short-lived Lakebase OAuth database credential. SQL is connected as that user, so database role and object grants apply. The app does not trust the forwarded email/name headers for authorization and never falls back to the app service principal in Databricks Apps.

Databricks Apps injects `DATABRICKS_HOST`, `DATABRICKS_APP_NAME`, and `DATABRICKS_APP_PORT`. The tracked `app.yaml.template` lists the runtime command and Lakebase settings: `PGHOST`, `PGPORT`, `PGDATABASE`, and `PGSSLMODE`. Copy it to the ignored `app.yaml` and replace the `PGHOST` placeholder with your endpoint host. `PGUSER` is derived per request from the authenticated Databricks identity; do not add a shared `PGPASSWORD` because the app mints a short-lived credential for each signed-in user. `LAKEBASE_ENDPOINT` is optional and skips endpoint discovery when set.

Lakebase OAuth requires a matching Postgres role for every Databricks user. The project owner role is created automatically; add OAuth roles for other users using the Lakebase `databricks_auth` extension before granting database/schema/object privileges.

## Run and deploy

### Run locally

Install `requirements.txt`, then authenticate to the target workspace with `databricks auth login --host <workspace-url> --profile <profile-name>`. Set `DATABRICKS_HOST`, `DATABRICKS_CONFIG_PROFILE`, `PGHOST`, and `PGDATABASE`, then run `python app.py`. The SDK uses the saved OAuth profile and handles token refresh. In Databricks Apps, the local profile path is disabled and requests require the forwarded user token.

### Deploy to Databricks

The following deploys this FastAPI app to the workspace selected by your Databricks CLI profile. Run it from the project root and replace the example profile and workspace URL with your own values. Never commit CLI credentials or workspace-specific configuration.

1. Authenticate with browser-based SSO if the profile is not already configured:

	```bash
	PROFILE="your-profile"
	WORKSPACE_URL="https://<your-workspace-host>"
	databricks auth login --host "$WORKSPACE_URL" --profile "$PROFILE"
	```

2. Create the app and upload a clean source directory. `workspace import-dir` does not exclude `.venv`, so stage the source without local Python artifacts:

	```bash
	PROFILE="your-profile"
	APP_NAME=lakebase-workbench
	cp app.yaml.template app.yaml
	# Replace the PGHOST placeholder in app.yaml with your Lakebase endpoint host.
	WORKSPACE_USER="$(databricks current-user me --profile "$PROFILE" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["userName"])')"
	WORKSPACE_PATH="/Workspace/Users/$WORKSPACE_USER/apps/$APP_NAME"
	STAGING_DIR="$(mktemp -d)"

	rsync -a --exclude='.git/' --exclude='.databricks/' --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' ./ "$STAGING_DIR"/
	databricks apps create "$APP_NAME" --description "Lakebase SQL Workbench" --profile "$PROFILE"
	databricks workspace mkdirs "$WORKSPACE_PATH" --profile "$PROFILE"
	databricks workspace import-dir "$STAGING_DIR" "$WORKSPACE_PATH" --overwrite --profile "$PROFILE"
	```

3. Open the app's authorization settings, enable **User authorization**, add the `postgres` scope, keep the default `iam.current-user:read` scope, and grant `CAN USE` only to intended users. A workspace admin may need to enable user authorization or allow the scope. Each user also needs a matching Lakebase OAuth role and the appropriate database grants.

4. Deploy and verify:

	```bash
	databricks apps deploy "$APP_NAME" --source-code-path "$WORKSPACE_PATH" --profile "$PROFILE"
	databricks apps get "$APP_NAME" --profile "$PROFILE" -o json
	databricks apps logs "$APP_NAME" --profile "$PROFILE"
	```

The tracked `app.yaml.template` starts `app.py`; the app binds to the runtime-provided `DATABRICKS_APP_PORT`. The workspace-specific `app.yaml` is ignored by Git and is included in the staged deployment source. Do not add a shared `PGPASSWORD`: the app mints a short-lived database credential for each signed-in user.

To redeploy after code changes, repeat the staging and `workspace import-dir` commands, then run `databricks apps deploy` again. Skip `databricks apps create` for an app that already exists. `--overwrite` is required when uploading updates.