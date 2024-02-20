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

import os
from typing import Generator

import pytest
import sqlalchemy
from langchain_core.messages.ai import AIMessage
from langchain_core.messages.human import HumanMessage

from langchain_google_alloydb_pg import AlloyDBChatMessageHistory, AlloyDBEngine

project_id = os.environ["PROJECT_ID"]
region = os.environ["REGION"]
cluster_id = os.environ["CLUSTER_ID"]
instance_id = os.environ["INSTANCE_ID"]
db_name = os.environ["DATABASE_ID"]


@pytest.fixture(name="memory_engine")
def setup() -> Generator:
    engine = AlloyDBEngine.from_instance(
        project_id=project_id, region=region, cluster=cluster_id, instance=instance_id, database=db_name
    )

    yield engine
    # use default table for AlloyDBChatMessageHistory
    table_name = "message_store"
    query = f"DROP TABLE IF EXISTS `{table_name}`"
    engine._aexecute(query)

def test_chat_message_history(memory_engine: AlloyDBEngine) -> None:
    history = AlloyDBChatMessageHistory(engine=memory_engine, session_id="test")
    history.add_user_message("hi!")
    history.add_ai_message("whats up?")
    messages = history.messages

    # verify messages are correct
    assert messages[0].content == "hi!"
    assert type(messages[0]) is HumanMessage
    assert messages[1].content == "whats up?"
    assert type(messages[1]) is AIMessage

    # verify clear() clears message history
    history.clear()
    assert len(history.messages) == 0


def test_chat_message_history_custom_table_name(memory_engine: AlloyDBEngine) -> None:
    """Test AlloyDBChatMessageHistory with custom table name"""
    history = AlloyDBChatMessageHistory(
        engine=memory_engine, session_id="test", table_name="message-store"
    )
    history.add_user_message("hi!")
    history.add_ai_message("whats up?")
    messages = history.messages

    # verify messages are correct
    assert messages[0].content == "hi!"
    assert type(messages[0]) is HumanMessage
    assert messages[1].content == "whats up?"
    assert type(messages[1]) is AIMessage

    # verify clear() clears message history
    history.clear()
    assert len(history.messages) == 0
