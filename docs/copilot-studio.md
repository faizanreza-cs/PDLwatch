# Use the Access Copilot from Microsoft Copilot Studio

The API's `/ask`, `/drugs/{drug}`, `/states/{state}` and `/changes` endpoints can be added to a
Copilot Studio agent as actions through a Power Platform custom connector. Market access, trade
and field teams then ask "Is Ozempic preferred in Ohio?" or "What changed in the Texas PDL this
quarter?" in Teams or Microsoft 365 Copilot, and every answer cites the PDL document and page.

## 1. Deploy the API

Run the Docker image anywhere with HTTPS (for example Azure Container Apps or App Service):

```bash
docker build -t pdlwatch .
docker run -p 8000:8000 \
  -e PDLWATCH_API_TOKEN=<a long random key> \
  -e PDLWATCH_PROVIDER=azure -e PDLWATCH_MODEL=<your Azure OpenAI deployment> \
  -e AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com -e AZURE_OPENAI_API_KEY=<key> \
  pdlwatch
```

With `PDLWATCH_API_TOKEN` set, every request must send it in the `X-API-Key` header. Without an
LLM configured, `/ask` still works in the offline mock mode, and the lookup endpoints never need
one.

The image ships with the synthetic corpus. To serve your own PDLs, point it at their warehouse:
mount a `pdlwatch.duckdb` built by `pdlwatch extract <folder>` or `pdlwatch sync` and set
`PDLWATCH_DB`, or set `PDLWATCH_BACKEND=snowflake` with the read-only role from
[`snowflake/setup.sql`](../snowflake/setup.sql).

## 2. Create the custom connector

1. In [`copilot/access-copilot.swagger.json`](../copilot/access-copilot.swagger.json), set `host`
   to your API's host name.
2. In Power Apps or Power Automate, go to **Custom connectors**, choose **Import an OpenAPI
   file** and upload that file. (It is OpenAPI 2.0, which the custom connector wizard accepts.)
3. On the **Security** step choose **API Key**, header `X-API-Key`.
4. Create the connector, then use **Test** with a question such as "Where is Mounjaro
   preferred?" and your key.

## 3. Add it to a Copilot Studio agent

1. Open your agent in Copilot Studio and add a tool (action) from the custom connector:
   **AskAccessCopilot**, plus **GetDrugAccess**, **GetStateFormulary** and **GetPdlChanges** for
   direct lookups.
2. Describe when to use it, for example: "Use for questions about Medicaid coverage of a drug:
   preferred or non-preferred status, prior authorization, step therapy, quantity limits, a
   state's drug list, and changes between PDL versions."
3. Test in the test pane, then publish to Teams.

Menu names change between Power Platform releases; see Microsoft's documentation for
[custom connectors](https://learn.microsoft.com/en-us/connectors/custom-connectors/) and
[Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/) if a step looks
different.

## Security notes

- Keep the key in the connector's connection, never in the OpenAPI file. Rotate it like any
  other secret.
- The connector's endpoints are read-only: they read the warehouse and never change it. The
  API's `/extract` upload endpoint is left out of the connector on purpose.
- SQL written by the agent is checked (one SELECT on the known tables, no file or table
  functions) and runs read-only with a row cap and a timeout.
