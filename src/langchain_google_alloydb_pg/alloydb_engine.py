# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Thread
from typing import TYPE_CHECKING, Awaitable, Dict, List, Optional, TypeVar, Union

import aiohttp
import google.auth  # type: ignore
import google.auth.transport.requests  # type: ignore
from google.cloud.alloydb.connector import AsyncConnector, IPTypes
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .version import __version__

if TYPE_CHECKING:
    import asyncpg  # type: ignore
    import google.auth.credentials  # type: ignore

T = TypeVar("T")

USER_AGENT = "langchain-google-alloydb-pg-python/" + __version__


async def _get_iam_principal_email(
    credentials: google.auth.credentials.Credentials,
) -> str:
    """Get email address associated with current authenticated IAM principal.

    Email will be used for automatic IAM database authentication to Cloud SQL.

    Args:
        credentials (google.auth.credentials.Credentials):
            The credentials object to use in finding the associated IAM
            principal email address.

    Returns:
        email (str):
            The email address associated with the current authenticated IAM
            principal.
    """
    # refresh credentials if they are not valid
    if not credentials.valid:
        request = google.auth.transport.requests.Request()
        credentials.refresh(request)
    if hasattr(credentials, "_service_account_email"):
        email = credentials._service_account_email
    # call OAuth2 api to get IAM principal email associated with OAuth2 token
    url = f"https://oauth2.googleapis.com/tokeninfo?access_token={credentials.token}"
    async with aiohttp.ClientSession() as client:
        response = await client.get(url, raise_for_status=True)
        response_json: Dict = await response.json()
        email = response_json.get("email")
    if email is None:
        raise ValueError(
            "Failed to automatically obtain authenticated IAM principal's "
            "email address using environment's ADC credentials!"
        )
    return email.replace(".gserviceaccount.com", "")


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool = True

    def __post_init__(self):
        if not isinstance(self.name, str):
            raise ValueError("Column name must be type string")
        if not isinstance(self.data_type, str):
            raise ValueError("Column data_type must be type string")


class AlloyDBEngine:
    """A class for managing connections to a Cloud SQL for Postgres database."""

    _connector: Optional[AsyncConnector] = None

    def __init__(
        self,
        engine: AsyncEngine,
        loop: Optional[asyncio.AbstractEventLoop],
        thread: Optional[Thread],
    ):
        self._engine = engine
        self._loop = loop
        self._thread = thread

    @classmethod
    def from_instance(
        cls,
        project_id: str,
        cluster: str,
        region: str,
        instance: str,
        database: str,
        user: Optional[str] = None,
        password: Optional[str] = None,
        ip_type: Union[str, IPTypes] = IPTypes.PUBLIC,
    ) -> AlloyDBEngine:
        # Running a loop in a background thread allows us to support
        # async methods from non-async environments
        loop = asyncio.new_event_loop()
        thread = Thread(target=loop.run_forever, daemon=True)
        thread.start()
        coro = cls._create(
            project_id,
            region,
            cluster,
            instance,
            database,
            ip_type,
            user,
            password,
            loop=loop,
            thread=thread,
        )
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    @classmethod
    async def _create(
        cls,
        project_id: str,
        region: str,
        cluster: str,
        instance: str,
        database: str,
        ip_type: Union[str, IPTypes],
        user: Optional[str] = None,
        password: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        thread: Optional[Thread] = None,
    ) -> AlloyDBEngine:
        # error if only one of user or password is set, must be both or neither
        if bool(user) ^ bool(password):
            raise ValueError(
                "Only one of 'user' or 'password' were specified. Either "
                "both should be specified to use basic user/password "
                "authentication or neither for IAM DB authentication."
            )

        if cls._connector is None:
            cls._connector = AsyncConnector(user_agent=USER_AGENT)

        if isinstance(ip_type, str):
            if ip_type.lower() == "public":
                ip_type = IPTypes.PUBLIC
            elif ip_type.lower() == "private":
                ip_type = IPTypes.PRIVATE
            else:
                raise ValueError("ip_type is not one of: public, private.")

        # if user and password are given, use basic auth
        if user and password:
            enable_iam_auth = False
            db_user = user
        # otherwise use automatic IAM database authentication
        else:
            # get application default credentials
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/userinfo.email"]
            )
            db_user = await _get_iam_principal_email(credentials)
            enable_iam_auth = True

        # anonymous function to be used for SQLAlchemy 'creator' argument
        async def getconn() -> asyncpg.Connection:
            conn = await cls._connector.connect(  # type: ignore
                f"projects/{project_id}/locations/{region}/clusters/{cluster}/instances/{instance}",
                "asyncpg",
                user=db_user,
                password=password,
                db=database,
                enable_iam_auth=enable_iam_auth,
                ip_type=ip_type,
            )
            return conn

        engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=getconn,
        )
        return cls(engine, loop, thread)

    @classmethod
    async def afrom_instance(
        cls,
        project_id: str,
        region: str,
        cluster: str,
        instance: str,
        database: str,
        user: Optional[str] = None,
        password: Optional[str] = None,
        ip_type: Union[str, IPTypes] = IPTypes.PUBLIC,
    ) -> AlloyDBEngine:
        return await cls._create(
            project_id,
            region,
            cluster,
            instance,
            database,
            ip_type,
            user,
            password,
        )

    async def _aexecute(self, query: str, params: Optional[dict] = None):
        """Execute a SQL query."""
        async with self._engine.connect() as conn:
            await conn.execute(text(query), params)
            await conn.commit()

    async def _aexecute_outside_tx(self, query: str):
        """Execute a SQL query."""
        async with self._engine.connect() as conn:
            await conn.execute(text("COMMIT"))
            await conn.execute(text(query))

    async def _afetch(self, query: str, params: Optional[dict] = None):
        async with self._engine.connect() as conn:
            """Fetch results from a SQL query."""
            result = await conn.execute(text(query), params)
            result_map = result.mappings()
            result_fetch = result_map.fetchall()

        return result_fetch

    def run_as_sync(self, coro: Awaitable[T]) -> T:
        if not self._loop:
            raise Exception("Engine was initialized async.")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def init_vectorstore_table(
        self,
        table_name: str,
        vector_size: int,
        content_column: str = "content",
        embedding_column: str = "embedding",
        metadata_columns: List[Column] = [],
        metadata_json_column: str = "langchain_metadata",
        id_column: str = "langchain_id",
        overwrite_existing: bool = False,
        store_metadata: bool = True,
    ) -> None:
        await self._aexecute("CREATE EXTENSION IF NOT EXISTS vector")

        if overwrite_existing:
            await self._aexecute(f'DROP TABLE IF EXISTS "{table_name}"')

        query = f"""CREATE TABLE "{table_name}"(
            "{id_column}" UUID PRIMARY KEY,
            "{content_column}" TEXT NOT NULL,
            "{embedding_column}" vector({vector_size}) NOT NULL"""
        for column in metadata_columns:
            query += f""",\n"{column.name}" {column.data_type}""" + (
                "NOT NULL" if not column.nullable else ""
            )
        if store_metadata:
            query += f',\n"{metadata_json_column}" JSON'
        query += "\n);"

        await self._aexecute(query)

    async def init_chat_history_table(self, table_name) -> None:
        create_table_query = f"""CREATE TABLE IF NOT EXISTS "{table_name}"(
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            data JSONB NOT NULL,
            type TEXT NOT NULL
        );"""
        await self._aexecute(create_table_query)
