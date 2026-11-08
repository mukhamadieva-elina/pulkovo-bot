import os

from pulkovo.config import Config
from pulkovo.web import create_app

app = create_app(Config.load())

if __name__ == "__main__":
    app.run(host="127.0.0.1",port=int(os.getenv("ADMIN_PORT","8080")),debug=False)
