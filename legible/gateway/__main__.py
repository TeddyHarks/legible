"""
python -m legible.gateway

Starts the Legible Coordination Gateway on localhost:8080.
"""
import sys


def main():
    try:
        import uvicorn
    except ImportError:
        print("\n  ✗ uvicorn not installed.")
        print("    pip install fastapi uvicorn\n")
        sys.exit(1)

    print("""
╔══════════════════════════════════════════════════════════════╗
║  Legible Coordination Gateway — V2 Shadow Mode               ║
║  RFC-0002 · Trustless Coordination Intelligence              ║
╚══════════════════════════════════════════════════════════════╝

  Mode:     Shadow (advisory only — no traffic interception)
  Docs:     http://127.0.0.1:8080/docs
  Status:   http://127.0.0.1:8080/status
  Decisions: http://127.0.0.1:8080/decisions

  Endpoints:
    POST /evaluate   — report outcome + get next recommendation
    POST /recommend  — get recommendation before a session
    POST /report     — report session outcome only
    GET  /status     — full gateway state
    GET  /decisions  — shadow decision log

  Logs:     logs/shadow_decisions.jsonl
""")

    uvicorn.run(
        "legible.gateway.app:app",
        host    = "127.0.0.1",
        port    = 8080,
        reload  = False,
        workers = 1,
    )


if __name__ == "__main__":
    main()