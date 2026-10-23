"""Build the customers-by-country DataStage flow used by minimal-tutorial.md §4.

    customers (PostgreSQL) --> by_country (Aggregator) --> Copy_1 --+--> customers_by_country (PostgreSQL)
                                                                    +--> customers_by_country.ds (Data Set)

You do not need this script to follow the tutorial: it imports the committed
customers-by-country.json. This is how that JSON was produced, for whoever wants to change it.

A flow that reads or writes a database needs *connector* stages, and their JSON shape is not
practical to write by hand (compare make-flow.py, which builds a flow with no connector). So this
script uses IBM's watsonx.data integration Python SDK, which builds the flow through the CP4D API:

    python3 -m venv .venv && .venv/bin/pip install ibm-watsonx-data-integration==2.1.0
    CPD_URL=https://<cpd-route> CPD_USER=<user> CPD_PASSWORD=<password> \
    PROJECT=<project-name> CONNECTION=customers-db .venv/bin/python fixture/build-flow.py

The project and the PostgreSQL connection must already exist (tutorial §4.2). Then export the flow
and put the placeholders back, so the committed file carries no project-specific ids:

    cpdctl dsjob get-flow --project <project-name> --name customers-by-country --output json \
      | sed -n '/^{/,$p' \
      | jq '(.. | objects | select(has("connection")) | .connection)
            |= (.ref = "__CONNECTION_ID__" | .project_ref = "__PROJECT_ID__")' \
      > fixture/customers-by-country.json

Two things the SDK does that are not obvious, learned the hard way:
  - an aggregation is a LIST OF ONE-KEY DICTS, not one dict: [{"reduce": col}, {"sum": out}, ...].
    A single dict crashes the SDK with "string indices must be integers".
  - the SDK leaves out properties left at their default value. For a Data Set stage the compiler
    then fails with 'Cannot invoke "java.util.List.get(int)" because "datasetmode" is null', so the
    update policy is set explicitly.
"""

import os

from ibm_watsonx_data_integration import Platform
from ibm_watsonx_data_integration.common.auth import ICP4DAuthenticator

url = os.environ["CPD_URL"]
auth = ICP4DAuthenticator(url, os.environ["CPD_USER"], os.environ["CPD_PASSWORD"], disable_ssl_verification=True)
platform = Platform(auth, base_url=url)
project = platform.projects.get(name=os.environ["PROJECT"])
conn = project.connections.get(name=os.environ["CONNECTION"])

fc = project.create_flow(name="customers-by-country", environment=None, description="", flow_type="batch")

source = fc.add_stage("PostgreSQL", "customers")
source.use_connection(conn)
source.configuration.schema_name = "public"
source.configuration.table_name = "customers"

agg = fc.add_stage("Aggregator", "by_country")
agg.configuration.key_properties = [{"key": "country"}]
agg.configuration.reduce_properties = [[{"reduce": "amount"}, {"sum": "total_amount"}, {"count": "customer_count"}]]

copy = fc.add_stage("Copy", "Copy_1")

# Truncate, then insert: running the job twice gives the same table.
target = fc.add_stage("PostgreSQL", "customers_by_country")
target.use_connection(conn)
target.configuration.schema_name = "public"
target.configuration.table_name = "customers_by_country"
target.configuration.table_action = "truncate"

dataset = fc.add_stage("Data set", "customers_by_country_ds")
dataset.configuration.file = "customers_by_country.ds"
dataset.configuration.update_policy = ">| [ds"  # overwrite — see the docstring

src_schema = source.connect_output_to(agg).set_name("Link_1").create_schema()
src_schema.add_field("INTEGER", "id")
src_schema.add_field("VARCHAR", "cust_name", length=20)
src_schema.add_field("CHAR", "country", length=2)
src_schema.add_field("DECIMAL", "amount", length=8, scale=2)

# The Aggregator outputs sum and count as doubles; PostgreSQL casts them into the target columns.
for link_name, src, dst in (("Link_2", agg, copy), ("Link_3", copy, target), ("Link_4", copy, dataset)):
    out = src.connect_output_to(dst).set_name(link_name).create_schema()
    out.add_field("CHAR", "country", length=2)
    out.add_field("DOUBLE", "customer_count")
    out.add_field("DOUBLE", "total_amount")

project.update_flow(fc)
print("created flow", fc.name)
