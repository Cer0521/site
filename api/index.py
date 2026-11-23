import sys
import traceback

try:
    from app import create_app
    app = create_app()
except Exception as e:
    err_msg = traceback.format_exc()
    print("FATAL ERROR ON STARTUP:\n", err_msg, file=sys.stderr)
    
    def error_app(environ, start_response):
        start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
        return [f"Application Startup Error:\n\n{err_msg}".encode("utf-8")]

    app = error_app
