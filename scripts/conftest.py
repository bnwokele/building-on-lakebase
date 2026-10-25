def pytest_addoption(parser):
    parser.addoption(
        "--agent-url",
        default="http://localhost:8000",
        help="Base URL of the agent server",
    )
    parser.addoption(
        "--token",
        default="",
        help="Bearer token for a deployed app (omit for a local server)",
    )
