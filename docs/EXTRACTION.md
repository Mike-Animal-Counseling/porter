**English** | [中文](EXTRACTION.zh-CN.md)

# Conversation extraction

`memory_capture` converts a supplied transcript into candidate memories. It
does not record conversations automatically. The calling tool decides when
to send text; each saved fact appears in the owner's inbox or follows triage.

The default `PORTER_LLM_EXTRACTOR=stub` uses deterministic bilingual keyword
rules and requires no language model. BGE-M3 remains the independent embedding
service used for retrieval. Most installations can keep these defaults.

## Optional local Ollama

Install a local model in your own Ollama service, then configure Porter:

```dotenv
PORTER_LLM_EXTRACTOR=ollama
PORTER_OLLAMA_MODEL=<installed-model-name>
PORTER_OLLAMA_URL=http://localhost:11434
PORTER_LLM_TIMEOUT=30
```

For Docker Desktop use `http://host.docker.internal:11434` for the URL. For
Linux containers, supply a reachable Ollama service address on your private
network. This repository does not install a model or enable Ollama by default.
Restart the API/stdio process after changing its environment. The model must
support Ollama chat structured outputs and `think=false`.
The extractor requests a 16K context and limits encoded source JSON to 8,000
UTF-8 bytes to leave room for instructions and output. Split longer transcripts
into smaller calls; oversized input is rejected before any model request.

The extractor sends only the supplied text to the configured service. Use a
local endpoint for local processing; a remote endpoint receives that text.
API and stdio must use the same extraction settings if both transports capture
conversations. HTTP MCP uses the API's settings.

Ollama output is size-limited and validated against a schema. Facts use a
fixed conservative confidence of 0.7 and **always queue for human review**,
including project facts. Model output cannot choose workspace/project tags or
permissions. Source tags come from the authenticated caller and still pass
workspace-role checks. PostgreSQL saves a capture batch transactionally;
schema/permission failures save no facts. Errors return `extractor_unavailable`
without including transcript text. The optional wiki layer uses the same
backend; an extraction outage falls back to ordinary search.

Implementation follows the [Ollama chat API](https://docs.ollama.com/api/chat).
OpenAI extraction is not implemented.

## Verification

Contract tests cover malformed output, timeouts, oversized responses, tag
injection, review requirements and background execution. An opt-in live check
uses a fixed synthetic transcript:

```powershell
$env:PORTER_TEST_OLLAMA_URL='http://localhost:11434'
$env:PORTER_TEST_OLLAMA_MODEL='<installed-model-name>'
python -m pytest -q tests/test_ollama_integration.py
```

On 2026-10-20, local Ollama 0.33.2 with Qwen3 0.6B returned valid structured
facts for four synthetic English/Chinese samples and completed the MCP review
workflow. It also incorrectly treated a greeting as a durable fact. This tiny
model verifies the integration; extraction quality still requires evaluation
on your intended data. The real sample is recorded in
[extraction-eval.2026-10-20.json](extraction-eval.2026-10-20.json).
