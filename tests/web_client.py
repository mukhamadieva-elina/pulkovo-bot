"""Native ASGI clients for route tests; login behavior is tested separately."""

import base64
import json

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from pulkovo.web import create_app


def authenticated_client(config, database, staff_name='Сотрудник'):
    client = TestClient(create_app(config, database), follow_redirects=False)
    session = dict(authenticated=True, csrf='test', staff_name=staff_name)
    value = TimestampSigner(config.secret_key).sign(base64.b64encode(json.dumps(session).encode())).decode()
    client.cookies.set('pulkovo_admin', value)
    return client
