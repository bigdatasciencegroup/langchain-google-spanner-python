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

import json
import logging
import warnings
import uuid
import datetime
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
)
from typing import Union
from google.cloud.spanner_v1 import param_types
from google.cloud.spanner_v1 import JsonObject
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from google.cloud import spanner
from google.cloud.spanner import Client
from langchain_core.utils import get_from_dict_or_env
from langchain_core.vectorstores import VectorStore
from enum import Enum
from abc import ABC, abstractmethod
from google.cloud.spanner_admin_database_v1.types import DatabaseDialect

logger = logging.getLogger(__name__)


ID_COLUMN_NAME = "id"
CONTENT_COLUMN_NAME = "content"
EMBEDDING_COLUMN_NAME = "embedding"
ADDITIONAL_METADATA_COLUMN_NAME = "metadata"


from dataclasses import dataclass

@dataclass
class TableColumn:
    """
    Represents column configuration, to be used as part of create DDL statement for table creation.

    Attributes:
        column_name (str): The name of the column.
        type (str): The type of the column.
        is_null (bool): Indicates whether the column allows null values.
    """
    name: str
    type: str
    is_null: bool = True


    def __post_init__(self):
        # Check if column_name is None after initialization
        if self.name is None:
            raise ValueError("column_name is mandatory and cannot be None.")

        if self.type is None:
            raise ValueError("type is mandatory and cannot be None.")

class DistanceStrategy(Enum):
    """
    Enum for distance calculation strategies.
    """
    COSINE = 1
    EUCLIDEIAN = 2


class DialectSemantics(ABC):
    """
    Abstract base class for dialect semantics.
    """
    @abstractmethod
    def getDistanceFunction(self, distance_strategy=DistanceStrategy.EUCLIDEIAN) -> str:
        """
        Abstract method to get the distance function based on the provided distance strategy.

        Parameters:
        - distance_strategy (DistanceStrategy): The distance calculation strategy. Defaults to DistanceStrategy.EUCLIDEAN.

        Returns:
        - str: The name of the distance function.
        """
        pass


class GoogleSqlSemnatics(DialectSemantics):
    """
    Implementation of dialect semantics for Google SQL.
    """
    def getDistanceFunction(self, distance_strategy=DistanceStrategy.EUCLIDEIAN) -> str:
        if distance_strategy == DistanceStrategy.COSINE:
            return "COSINE_DISTANCE"

        return "EUCLIDEAN_DISTANCE"


class PGSqlSemnatics(DialectSemantics):
    """
    Implementation of dialect semantics for PostgreSQL.
    """
    def getDistanceFunction(self, distance_strategy=DistanceStrategy.EUCLIDEIAN) -> str:
        if distance_strategy == DistanceStrategy.COSINE:
            return "spanner.cosine_distance"
        return "spanner.euclidean_distance"


class QueryParameters:
    """
    Class representing query parameters for nearest neighbors search.
    """
    class NearestNeighborsAlgorithm(Enum):
        """
        Enum for nearest neighbors search algorithms.
        """
        BRUTE_FORCE = 1

    def __init__(
        self,
        algorithm=NearestNeighborsAlgorithm.BRUTE_FORCE,
        distance_strategy=DistanceStrategy.EUCLIDEIAN,
        staleness=0,
    ):
        """
        Initialize query parameters.

        Parameters:
        - algorithm (NearestNeighborsAlgorithm): The nearest neighbors search algorithm. Defaults to NearestNeighborsAlgorithm.BRUTE_FORCE.
        - distance_strategy (DistanceStrategy): The distance calculation strategy. Defaults to DistanceStrategy.EUCLIDEAN.
        - staleness (int): The staleness value. Defaults to 0.
        """
        self.algorithm = algorithm
        self.distance_strategy = distance_strategy
        self.staleness = staleness


class SpannerVectorStore(VectorStore):
    """
    A class for managing vector stores in Google Cloud Spanner.
    """
    @staticmethod
    def init_vector_store_table(
        instance_id: str,
        database_id: str,
        table_name: str,
        client: Client = Client(project="span-cloud-testing"),
        id_column: Union[str, TableColumn] = ID_COLUMN_NAME,
        content_column: str = CONTENT_COLUMN_NAME,
        embedding_column: str = EMBEDDING_COLUMN_NAME,
        metadata_columns: Optional[List[TableColumn]] = None,
        vector_size: Optional[int] = None,
    ):
        """
        Initialize the vector store new table in Google Cloud Spanner.

        Parameters:
        - instance_id (str): The ID of the Spanner instance.
        - database_id (str): The ID of the Spanner database.
        - table_name (str): The name of the table to initialize.
        - client (Client): The Spanner client. Defaults to Client(project="span-cloud-testing").
        - id_column (str): The name of the row ID column. Defaults to ID_COLUMN_NAME.
        - content_column (str): The name of the content column. Defaults to CONTENT_COLUMN_NAME.
        - embedding_column (str): The name of the embedding column. Defaults to EMBEDDING_COLUMN_NAME.
        - metadata_columns (Optional[List[Tuple]]): List of tuples containing metadata column information. Defaults to None.
        - vector_size (Optional[int]): The size of the vector. Defaults to None.
        """

        instance = client.instance(instance_id)

        if not instance.exists():
            raise Exception("Instance with id-{} doesn't exist.".format(instance_id))

        database = instance.database(database_id)

        if not database.exists():
            raise Exception("Database with id-{} doesn't exist.".format(database_id))

        database.reload()
        print (database, database.database_dialect)

        ddl = SpannerVectorStore._generate_sql(
            database.database_dialect,
            table_name,
            id_column,
            content_column,
            embedding_column,
            metadata_columns,
        )

        print (ddl)

        operation = database.update_ddl([ddl])

        print("Waiting for operation to complete...")
        operation.result(100000)

    @staticmethod
    def _generate_sql(
        dialect,
        table_name,
        id_column,
        content_column,
        embedding_column,
        column_configs,
    ):
        """
        Generate SQL for creating the vector store table.

        Parameters:
        - dialect: The database dialect.
        - table_name: The name of the table.
        - id_column: The name of the row ID column.
        - content_column: The name of the content column.
        - embedding_column: The name of the embedding column.
        - column_names: List of tuples containing metadata column information.

        Returns:
        - str: The generated SQL.
        """
        sql = f"CREATE TABLE {table_name} (\n"

        if dialect == DatabaseDialect.POSTGRESQL:
            column_sql = (
                f"  {id_column} varchar(36) DEFAULT (spanner.generate_uuid()),\n"
            )
            sql += column_sql
            column_sql = f"  {content_column} text,\n"
            sql += column_sql
            column_sql = f"  {embedding_column} float8[],\n"
            sql += column_sql
        else:
            column_sql = f"  {id_column} STRING(36) DEFAULT (GENERATE_UUID()),\n"
            sql += column_sql
            column_sql = f"  {content_column} STRING(MAX),\n"
            sql += column_sql
            column_sql = f"  {embedding_column} ARRAY<FLOAT64>,\n"
            sql += column_sql

        if column_configs is not None:
            for column_config in column_configs:
                # Append column name and data type
                column_sql = f"  {column_config.name} {column_config.type}"

                # Add nullable constraint if specified
                if not column_config.is_null:
                   column_sql += " NOT NULL"

                # Add a comma and a newline for the next column
                column_sql += ",\n"
                sql += column_sql

        # Remove the last comma and newline, add closing parenthesis
        if dialect == DatabaseDialect.POSTGRESQL:
             sql +=  "  PRIMARY KEY(" + id_column + ")\n)"
        else:
            sql = sql.rstrip(",\n") + "\n) PRIMARY KEY(" + id_column + ")"

        return sql

    def __init__(
        self,
        instance_id: str,
        database_id: str,
        table_name: str,
        embedding_service: Embeddings,
        id_column: str = ID_COLUMN_NAME,
        content_column: str = CONTENT_COLUMN_NAME,
        embedding_column: str = EMBEDDING_COLUMN_NAME,
        client: Client = Client(),
        metadata_columns: Optional[List[str]] = None,
        ignore_metadata_columns: Optional[List[str]] = None,
        metadata_json_column: Optional[str] = None,
        query_parameters: QueryParameters = QueryParameters(),
    ):
        """
        Initialize the SpannerVectorStore.

        Parameters:
        - instance_id (str): The ID of the Spanner instance.
        - database_id (str): The ID of the Spanner database.
        - table_name (str): The name of the table.
        - embedding_service (Embeddings): The embedding service.
        - id_column (str): The name of the row ID column. Defaults to ID_COLUMN_NAME.
        - content_column (str): The name of the content column. Defaults to CONTENT_COLUMN_NAME.
        - embedding_column (str): The name of the embedding column. Defaults to EMBEDDING_COLUMN_NAME.
        - client (Client): The Spanner client. Defaults to Client().
        - metadata_columns (Optional[List[str]]): List of metadata columns. Defaults to None.
        - ignore_metadata_columns (Optional[List[str]]): List of metadata columns to ignore. Defaults to None.
        - metadata_json_column (Optional[str]): The generic metadata column. Defaults to None.
        - query_parameters (QueryParameters): The query parameters. Defaults to QueryParameters().
        """
        self._instance_id = instance_id
        self._database_id = database_id
        self._table_name = table_name
        self._client = client
        self._id_column = id_column
        self._content_column = content_column
        self._embedding_column = embedding_column
        self._metadata_json_column = metadata_json_column

        self._query_parameters = query_parameters
        self._embedding_service = embedding_service

        instance = self._client.instance(instance_id)

        if not instance.exists():
            raise Exception("Instance with id-{} doesn't exist.".format(instance_id))

        self._database = instance.database(database_id)

        self._database.reload()

        self._dialect_semantics = (
            PGSqlSemnatics()
            if self._database.database_dialect == DatabaseDialect.POSTGRESQL
            else GoogleSqlSemnatics()
        )

        if not self._database.exists():
            raise Exception("Database with id-{} doesn't exist.".format(database_id))


        table = self._database.table(table_name)

        if not table.exists():
            raise Exception("Table with name-{} doesn't exist.".format(table_name))


        column_type_map = {column.name: column for column in table.schema}
        self._validate_table_schema(column_type_map)

        default_columns = [id_column, content_column, embedding_column]

        columns_to_insert  = [] + default_columns

        if ignore_metadata_columns is not None:
            columns_to_insert = [element for element in column_type_map.keys() if element not in ignore_metadata_columns]
            self._metadata_columns = [item for item in columns_to_insert if item not in default_columns]
        else:
            self._metadata_columns = []
            if metadata_columns is not None:
                columns_to_insert.extend(metadata_columns)
                self._metadata_columns.extend(metadata_columns)

            if metadata_json_column is not None and metadata_json_column not in columns_to_insert:
                columns_to_insert.append(metadata_json_column)
                self._metadata_columns.append(metadata_json_column)

        self._columns_to_insert = columns_to_insert

    def _validate_table_schema(self, column_type_map):
        # check for page_content and embedding type
        # check whether metdata columns are present
        pass

    def _select_relevance_score_fn(self) -> Callable[[float], float]:
            if self._query_parameters.distance_strategy == DistanceStrategy.COSINE:
                 return self._cosine_relevance_score_fn
            elif self._query_parameters.distance_strategy == DistanceStrategy.EUCLIDEIAN:
                return self._euclidean_relevance_score_fn
            else:
                raise ValueError(
                    "Unknown distance strategy, must be cosine, max_inner_product,"
                    " or euclidean"
                )
    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
        batch_size: int = 5000,
        **kwargs: Any,
    ) -> List[str]:
        """
        Add texts to the vector store index.

        Args:
            texts (Iterable[str]): Iterable of strings to add to the vector store.
            metadatas (Optional[List[dict]]): Optional list of metadatas associated with the texts.
            ids (Optional[List[str]]): Optional list of IDs for the texts.
            batch_size (int): The batch size for inserting data. Defaults to 5000.

        Returns:
            List[str]: List of IDs of the added texts.
        """
        texts_list = list(texts)
        number_of_records = len(texts_list)

        if number_of_records == 0:
            return []

        if ids is not None:
            assert len(ids) == number_of_records

        if metadatas is not None:
            assert len(metadatas) == number_of_records

        embeds = self._embedding_service.embed_documents(texts_list)

        if metadatas is None:
            metadatas = [{} for _ in texts]

        values_dict: dict = {key: [] for key in self._metadata_columns}

        if metadatas:
            for row_metadata in metadatas:
                if self._metadata_json_column is not None:
                    row_metadata[self._metadata_json_column] =  JsonObject(row_metadata)

                for column_name in self._metadata_columns:
                    if row_metadata.get(column_name) is not None:
                        values_dict[column_name].append(row_metadata[column_name])
                    else:
                        values_dict[column_name].append(None)

        if ids is not None:
            values_dict[self._id_column] = ids

        values_dict[self._content_column] = texts
        values_dict[self._embedding_column] = embeds


        columns_to_insert = values_dict.keys();

        rows_to_insert = [
            [values_dict[key][i] for key in values_dict]
            for i in range(number_of_records)
        ]

        for i in range(0, len(rows_to_insert), batch_size):
            batch = rows_to_insert[i : i + batch_size]
            self._insert_data(batch, columns_to_insert)

        return ids if ids is not None else []

    def _insert_data(self, records, columns_to_insert):
        with self._database.batch() as batch:
            batch.insert(
                table=self._table_name,
                columns=columns_to_insert,
                values=records,
            )

    def add_documents(
        self, documents: List[Document], ids: Optional[List[str]] = None, **kwargs: Any
    ) -> List[str]:
        """
        Add documents to the vector store.

        Args:
            documents (List[Document]): Documents to add to the vector store.
            ids (Optional[List[str]]): Optional list of IDs for the documents.

        Returns:
            List[str]: List of IDs of the added texts.
        """
        texts = [doc.page_content for doc in documents]
        metadatas = [doc.metadata for doc in documents]
        return self.add_texts(texts=texts, metadatas=metadatas, ids=ids, **kwargs)

    def delete(
        self,
        ids: Optional[List[str]] = None,
        documents: Optional[List[Document]] = None,
        **kwargs: Any,
    ) -> Optional[bool]:
        """
        Delete records from the vector store.

        Args:
            ids (Optional[List[str]]): List of IDs to delete.
            documents (Optional[List[Document]]): List of documents to delete.

        Returns:
            Optional[bool]: True if deletion is successful, False otherwise, None if not implemented.
        """
        if (ids is None and documents is None):
             raise Exception("Pass id/documents to delete")

        columns = []
        values: List[Any] = []

        if ids is not None:
            columns = [self._id_column]
            values = ['(\'' + value + '\')' for value in ids]
        elif documents is not None:
            pass
            #ToDo: Pick it up later
            # columns = [self._content_column] + self._metadata_columns

            # if (self._metadata_json_column is not None):
            #     columns.remove(self._metadata_json_column)

            # for doc in documents:
            #     value: List[Any] = []
            #     value.append(doc.page_content)

            #     for column_name in self._metadata_columns:
            #         value.append(doc.metadata.get(column_name))
            #     values.append('(' +  ss + ')')


        def delete_records(transaction):
            column_expression = '(' + ', '.join(columns) + ')'

            sql_query = """
                DELETE FROM {table_name} 
                WHERE {column_expression} IN {struct_placeholder}
                """.format(
                    table_name=self._table_name,
                    column_expression=column_expression,
                    struct_placeholder=', '.join([f'({elem})' for elem in values])
                )

            print (sql_query, values)

            results = transaction.execute_update(
                dml=sql_query
            )

            print (results)

        self._database.run_in_transaction(delete_records)

        return True

    def similarity_search_with_score_by_vector(
        self, embedding: List[float], k: int = 4, pre_filter: Optional[str] = None, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        """
        Perform similarity search for a given query.

        Args:
            query (str): The query string.
            k (int): The number of nearest neighbors to retrieve. Defaults to 4.
            pre_filter (Optional[str]): Pre-filter condition for the query. Defaults to None.

        Returns:
            List[Document]: List of documents most similar to the query.
        """
        DISTANCE_SELECT_QUERY_NAME = "distance"

        staleness = self._query_parameters.staleness
        staleness = datetime.timedelta(seconds=staleness)

        distance_function = self._dialect_semantics.getDistanceFunction(
            self._query_parameters.distance_strategy
        )

        parameter = ("@vector_embedding", "vector_embedding")

        if (self._database.database_dialect == DatabaseDialect.POSTGRESQL):
             parameter = ("$1", "p1")

        select_column_names = ",".join(self._columns_to_insert) + ","
        column_order_map = {value: index for index, value in enumerate(self._columns_to_insert)}
        column_order_map[DISTANCE_SELECT_QUERY_NAME] = len(self._columns_to_insert)


        sql_query = """
            SELECT {select_column_names} {distance_function}({embedding_column}, {vector_embedding_placeholder}) AS {distance_alias}
            FROM {table_name} 
            WHERE {filter}
            ORDER BY distance
            LIMIT {k_count};
        """.format(
            table_name=self._table_name,
            embedding_column=self._embedding_column,
            select_column_names=select_column_names,
            vector_embedding_placeholder=parameter[0],
            filter=pre_filter if pre_filter is not None else "1 = 1",
            k_count=k,
            distance_function=distance_function,
            distance_alias=DISTANCE_SELECT_QUERY_NAME,
        )

        with self._database.snapshot(exact_staleness=staleness) as snapshot:
            results = snapshot.execute_sql(
                sql=sql_query,
                params={parameter[1]: embedding},
                param_types={
                   parameter[1]: param_types.Array(param_types.FLOAT64)
                },
            )

            documents = []
            for row in results:
                page_content = row[column_order_map[self._content_column]]

                if self._metadata_json_column is not None and row[column_order_map[self._metadata_json_column]]:
                    metadata = row[column_order_map[self._metadata_json_column]]
                else:
                    metadata = {
                        key: row[column_order_map[key]]
                        for key in self._metadata_columns
                        if row[column_order_map[key]] is not None
                    }

                doc = Document(page_content=page_content, metadata=metadata)
                documents.append((doc, row[column_order_map[DISTANCE_SELECT_QUERY_NAME]]))

        return documents

    def similarity_search(
        self, query: str, k: int = 4, pre_filter: Optional[str] = None, **kwargs: Any
    ) -> List[Document]:
        """
        Perform similarity search for a given query.

        Args:
            query (str): The query string.
            k (int): The number of nearest neighbors to retrieve. Defaults to 4.
            pre_filter (Optional[str]): Pre-filter condition for the query. Defaults to None.

        Returns:
            List[Document]: List of documents most similar to the query.
        """
        embedding = self._embedding_service.embed_query(query)
        documents = self.similarity_search_with_score_by_vector(
            embedding=embedding, k=k, pre_filter = pre_filter
        )
        return [doc for doc, _ in documents]

    def similarity_search_with_score(
        self, query: str, k: int = 4, pre_filter: Optional[str] = None, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        """
        Perform similarity search for a given query with scores.

        Args:
            query (str): The query string.
            k (int): The number of nearest neighbors to retrieve. Defaults to 4.
            pre_filter (Optional[str]): Pre-filter condition for the query. Defaults to None.

        Returns:
            List[Tuple[Document, float]]: List of tuples containing Document and similarity score.
        """
        embedding = self._embedding_service.embed_query(query)
        documents = self.similarity_search_with_score_by_vector(
            embedding=embedding, k=k, pre_filter = pre_filter
        )
        return documents

    def similarity_search_by_vector(
        self, embedding: List[float], k: int = 4, pre_filter: Optional[str] = None, **kwargs: Any
    ) -> List[Document]:
        """
        Perform similarity search by vector.

        Args:
            embedding (List[float]): The embedding vector.
            k (int): The number of nearest neighbors to retrieve. Defaults to 4.
            pre_filter (Optional[str]): Pre-filter condition for the query. Defaults to None.

        Returns:
            List[Document]: List of documents most similar to the query.
        """
        documents = self.similarity_search_with_score_by_vector(
            embedding=embedding, k=k, pre_filter = pre_filter
        )
        return [doc for doc, _ in documents]

    @classmethod
    def from_documents(
        cls: Type[SpannerVectorStore],
        documents: List[Document],
        embedding: Embeddings,
        id_column: str = ID_COLUMN_NAME,
        content_column: str = CONTENT_COLUMN_NAME,
        embedding_column: str = EMBEDDING_COLUMN_NAME,
        ids: Optional[List[str]] = None,
        client: Client = Client(),
        metadata_columns: Optional[List[str]] = None,
        ignore_metadata_columns: Optional[List[str]] = None,
        metadata_json_column: Optional[str] = None,
        query_parameter: QueryParameters = QueryParameters(),
        **kwargs: Any,
    ) -> SpannerVectorStore:
        """
        Initialize SpannerVectorStore from a list of documents.

        Args:
            documents (List[Document]): List of documents.
            embedding (Embeddings): The embedding service.
            id_column (str): The name of the row ID column. Defaults to ID_COLUMN_NAME.
            content_column (str): The name of the content column. Defaults to CONTENT_COLUMN_NAME.
            embedding_column (str): The name of the embedding column. Defaults to EMBEDDING_COLUMN_NAME.
            ids (Optional[List[str]]): Optional list of IDs for the documents. Defaults to None.
            client (Client): The Spanner client. Defaults to Client().
            metadata_columns (Optional[List[str]]): List of metadata columns. Defaults to None.
            ignore_metadata_columns (Optional[List[str]]): List of metadata columns to ignore. Defaults to None.
            metadata_json_column (Optional[str]): The generic metadata column. Defaults to None.
            query_parameter (QueryParameters): The query parameters. Defaults to QueryParameters().

        Returns:
            SpannerVectorStore: Initialized SpannerVectorStore instance.
        """
        texts = [d.page_content for d in documents]
        metadatas = [d.metadata for d in documents]
        return cls.from_texts(
            texts,
            embedding,
            metadatas=metadatas,
            embedding_service=embedding,
            id_column=id_column,
            content_column=content_column,
            embedding_column=embedding_column,
            client=client,
            ids=ids,
            metadata_columns=metadata_columns,
            ignore_metadata_columns=ignore_metadata_columns,
            metadata_json_column=metadata_json_column,
            query_parameter=query_parameter,
            kwargs=kwargs,
        )

    @classmethod
    def from_texts(
        cls: Type[SpannerVectorStore],
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        id_column: str = ID_COLUMN_NAME,
        content_column: str = CONTENT_COLUMN_NAME,
        embedding_column: str = EMBEDDING_COLUMN_NAME,
        ids: Optional[List[str]] = None,
        client: Client = Client(),
        metadata_columns: Optional[List[str]] = None,
        ignore_metadata_columns: Optional[List[str]] = None,
        metadata_json_column: Optional[str] = None,
        query_parameter: QueryParameters = QueryParameters(),
        **kwargs: Any,
    ) -> SpannerVectorStore:
        """
        Initialize SpannerVectorStore from a list of texts.

        Args:
            texts (List[str]): List of texts.
            embedding (Embeddings): The embedding service.
            metadatas (Optional[List[dict]]): Optional list of metadatas associated with the texts. Defaults to None.
            id_column (str): The name of the row ID column. Defaults to ID_COLUMN_NAME.
            content_column (str): The name of the content column. Defaults to CONTENT_COLUMN_NAME.
            embedding_column (str): The name of the embedding column. Defaults to EMBEDDING_COLUMN_NAME.
            ids (Optional[List[str]]): Optional list of IDs for the texts. Defaults to None.
            client (Client): The Spanner client. Defaults to Client().
            metadata_columns (Optional[List[str]]): List of metadata columns. Defaults to None.
            ignore_metadata_columns (Optional[List[str]]): List of metadata columns to ignore. Defaults to None.
            metadata_json_column (Optional[str]): The generic metadata column. Defaults to None.
            query_parameter (QueryParameters): The query parameters. Defaults to QueryParameters().

        Returns:
            SpannerVectorStore: Initialized SpannerVectorStore instance.
        """
        instance_id = get_from_dict_or_env(
            data=kwargs,
            key="instance_id",
            env_key="SPANNER_INSTANCE_ID",
        )

        database_id = get_from_dict_or_env(
            data=kwargs,
            key="database_id",
            env_key="SPANNER_DATABASE_ID",
        )

        table_name = get_from_dict_or_env(
            data=kwargs,
            key="table_name",
            env_key="SPANNER_TABLE_NAME",
        )

        store = cls(
            instance_id=instance_id,
            database_id=database_id,
            table_name=table_name,
            embedding_service=embedding,
            id_column=id_column,
            content_column=content_column,
            embedding_column=embedding_column,
            client=client,
            metadata_columns=metadata_columns,
            ignore_metadata_columns=ignore_metadata_columns,
            metadata_json_column=metadata_json_column,
            query_parameters=query_parameter,
        )

        store.add_texts(texts=texts, metadatas=metadatas, ids=ids)

        return store