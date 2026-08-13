# Fixed Persona / MatrAIx runner

P1 accepts any runner that implements one request per process:

```text
JSON object on stdin → JSON object on stdout
```

The bundled `scripts/persona_runner.py` is an OpenAI-compatible adapter. It
works with a hosted provider, a self-hosted Persona 8B gateway, or a MatrAIx
gateway that exposes `/v1/chat/completions`.

Configure locally in `.env` or the shell:

```bash
export PERSONA_API_URL=http://127.0.0.1:8000/v1/chat/completions
export PERSONA_API_KEY=local-only
export PERSONA_MODEL=persona-8b
```

`MATRAIX_API_URL`, `MATRAIX_API_KEY`, and `MATRAIX_MODEL` are accepted aliases.
No credentials are written to benchmark artifacts.

Run a prepared manifest:

```bash
python scripts/geo.py feedback-benchmark run \
  --manifest benchmark/manifest.json \
  --runner "python scripts/persona_runner.py" \
  --output benchmark/persona_results.jsonl

python scripts/geo.py feedback-benchmark analyze \
  --manifest benchmark/manifest.json
```

The adapter injects the frozen persona profile into the system prompt, uses
temperature `0` and the supplied seed, and requires a JSON response with:

```json
{
  "close_ready": true,
  "reopen_required": false,
  "evidence_complete": true,
  "owner_role": "content_owner",
  "next_action": null,
  "rationale": "..."
}
```

Provider errors, timeouts, invalid JSON, and missing fields fail closed. They
are recorded as invalid trials and cannot contribute to a publishable summary.
The runner never computes Ground Truth; that remains the deterministic runtime
truth adapter's responsibility.
