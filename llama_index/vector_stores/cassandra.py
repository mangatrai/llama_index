"""Cassandra / Astra DB Vector store index.

An index based on a DB table with vector search capabilities,
powered by the cassIO library

"""

import logging, uuid, os
from typing import Any, Dict, Iterable, List, Optional, TypeVar, cast
from traceloop.sdk import Traceloop
from traceloop.sdk.tracing import tracing as Tracer
from traceloop.sdk.decorators import workflow, task, agent

from llama_index.indices.query.embedding_utils import (
    get_top_k_mmr_embeddings,
)
from llama_index.schema import BaseNode, MetadataMode
from llama_index.vector_stores.types import (
    ExactMatchFilter,
    MetadataFilters,
    VectorStore,
    VectorStoreQuery,
    VectorStoreQueryMode,
    VectorStoreQueryResult,
)
from llama_index.vector_stores.utils import (
    metadata_dict_to_node,
    node_to_metadata_dict,
)

_logger = logging.getLogger(__name__)

DEFAULT_MMR_PREFETCH_FACTOR = 4.0
DEFAULT_INSERTION_BATCH_SIZE = 20

T = TypeVar("T")

"""Adding Open Telemetry Observability
"""
#change implementation of api key
TRACELOOP_API_KEY=os.getenv('TRACELOOP_API_KEY')
Traceloop.init(app_name="CassandraVectorStore_LlamaIndex", disable_batch=True)
# Generate a UUID
uuid_obj = str(uuid.uuid4())
Tracer.set_correlation_id(uuid_obj)

def _batch_iterable(iterable: Iterable[T], batch_size: int) -> Iterable[Iterable[T]]:
    this_batch = []
    for entry in iterable:
        this_batch.append(entry)
        if len(this_batch) == batch_size:
            yield this_batch
            this_batch = []
    if this_batch:
        yield this_batch

@agent(name="CassandraVectorStore", method_name="__init__")
class CassandraVectorStore(VectorStore):
    """Cassandra Vector Store.

    An abstraction of a Cassandra table with
    vector-similarity-search. Documents, and their embeddings, are stored
    in a Cassandra table and a vector-capable index is used for searches.
    The table does not need to exist beforehand: if necessary it will
    be created behind the scenes.

    All Cassandra operations are done through the cassIO library.

    Args:
        session (cassandra.cluster.Session): the Cassandra session to use
        keyspace (str): name of the Cassandra keyspace to work in
        table (str): table name to use. If not existing, it will be created.
        embedding_dimension (int): length of the embedding vectors in use.
        ttl_seconds (Optional[int]): expiration time for inserted entries.
            Default is no expiration.

    """

    stores_text: bool = True
    flat_metadata: bool = True

    @task(name="Establish Cassandra Connection")
    def __init__(
        self,
        session: Any,
        keyspace: str,
        table: str,
        embedding_dimension: int,
        ttl_seconds: Optional[int] = None,
        insertion_batch_size: int = DEFAULT_INSERTION_BATCH_SIZE,
    ) -> None:
        import_err_msg = "`cassio` package not found, please run `pip install cassio`"
        try:
            from cassio.table import ClusteredMetadataVectorCassandraTable
        except ImportError:
            raise ImportError(import_err_msg)

        self._session = session
        self._keyspace = keyspace
        self._table = table
        self._embedding_dimension = embedding_dimension
        self._ttl_seconds = ttl_seconds
        self._insertion_batch_size = insertion_batch_size

        _logger.debug("Creating the Cassandra table")
        self.vector_table = ClusteredMetadataVectorCassandraTable(
            session=self._session,
            keyspace=self._keyspace,
            table=self._table,
            vector_dimension=self._embedding_dimension,
            primary_key_type=["TEXT", "TEXT"],
            # a conservative choice here, to make everything searchable
            # except the bulky "_node_content" key (it'd make little sense to):
            metadata_indexing=("default_to_searchable", ["_node_content"]),
        )

    @task(name="Adding rows to the vector store")
    def add(
        self,
        nodes: List[BaseNode],
    ) -> List[str]:
        """Add nodes to index.

        Args:
            nodes: List[BaseNode]: list of node with embeddings

        """
        node_ids = []
        node_contents = []
        node_metadatas = []
        node_embeddings = []
        for node in nodes:
            metadata = node_to_metadata_dict(
                node,
                remove_text=True,
                flat_metadata=self.flat_metadata,
            )
            node_ids.append(node.node_id)
            node_contents.append(node.get_content(metadata_mode=MetadataMode.NONE))
            node_metadatas.append(metadata)
            node_embeddings.append(node.get_embedding())

        _logger.debug(f"Adding {len(node_ids)} rows to table")
        # Concurrent batching of inserts:
        insertion_tuples = zip(node_ids, node_contents, node_metadatas, node_embeddings)
        for insertion_batch in _batch_iterable(
            insertion_tuples, batch_size=self._insertion_batch_size
        ):
            futures = []
            for (
                node_id,
                node_content,
                node_metadata,
                node_embedding,
            ) in insertion_batch:
                node_ref_doc_id = node_metadata["ref_doc_id"]
                futures.append(
                    self.vector_table.put_async(
                        row_id=node_id,
                        body_blob=node_content,
                        vector=node_embedding,
                        metadata=node_metadata,
                        partition_id=node_ref_doc_id,
                        ttl_seconds=self._ttl_seconds,
                    )
                )
            for future in futures:
                _ = future.result()

        return node_ids

    @task(name="Deleting Data From DB Tables")
    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        """
        Delete nodes using with ref_doc_id.

        Args:
            ref_doc_id (str): The doc_id of the document to delete.

        """
        _logger.debug("Deleting a document from the Cassandra table")
        self.vector_table.delete_partition(
            partition_id=ref_doc_id,
        )

    @property
    def client(self) -> Any:
        """Return the underlying cassIO vector table object."""
        return self.vector_table

    @staticmethod
    def _query_filters_to_dict(query_filters: MetadataFilters) -> Dict[str, Any]:
        if any(not isinstance(f, ExactMatchFilter) for f in query_filters.filters):
            raise NotImplementedError("Only `ExactMatchFilter` filters are supported")
        return {f.key: f.value for f in query_filters.filters}

    @task(name="Query Vector Database")
    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        """Query index for top k most similar nodes.

        Supported query modes: 'default' (most similar vectors) and 'mmr'.

        Args:
            query (VectorStoreQuery): the basic query definition. Defines:
                mode (VectorStoreQueryMode): one of the supported modes
                query_embedding (List[float]): query embedding to search against
                similarity_top_k (int): top k most similar nodes
                mmr_threshold (Optional[float]): this is the 0-to-1 MMR lambda.
                    If present, takes precedence over the kwargs parameter.
                    Ignored unless for MMR queries.

        Args for query.mode == 'mmr' (ignored otherwise):
            mmr_threshold (Optional[float]): this is the 0-to-1 lambda for MMR.
                Note that in principle mmr_threshold could come in the query
            mmr_prefetch_factor (Optional[float]): factor applied to top_k
                for prefetch pool size. Defaults to 4.0
            mmr_prefetch_k (Optional[int]): prefetch pool size. This cannot be
                passed together with mmr_prefetch_factor

        """
        _available_query_modes = [
            VectorStoreQueryMode.DEFAULT,
            VectorStoreQueryMode.MMR,
        ]
        if query.mode not in _available_query_modes:
            raise NotImplementedError(f"Query mode {query.mode} not available.")
        #
        query_embedding = cast(List[float], query.query_embedding)

        # metadata filtering
        if query.filters is not None:
            # raise NotImplementedError("No metadata filtering yet")
            query_metadata = self._query_filters_to_dict(query.filters)
        else:
            query_metadata = {}

        _logger.debug(
            f"Running ANN search on the Cassandra table (query mode: {query.mode})"
        )
        if query.mode == VectorStoreQueryMode.DEFAULT:
            matches = list(
                self.vector_table.metric_ann_search(
                    vector=query_embedding,
                    n=query.similarity_top_k,
                    metric="cos",
                    metric_threshold=None,
                    metadata=query_metadata,
                )
            )
            top_k_scores = [match["distance"] for match in matches]
        elif query.mode == VectorStoreQueryMode.MMR:
            # Querying a larger number of vectors and then doing MMR on them.
            if (
                kwargs.get("mmr_prefetch_factor") is not None
                and kwargs.get("mmr_prefetch_k") is not None
            ):
                raise ValueError(
                    "'mmr_prefetch_factor' and 'mmr_prefetch_k' "
                    "cannot coexist in a call to query()"
                )
            else:
                if kwargs.get("mmr_prefetch_k") is not None:
                    prefetch_k0 = int(kwargs["mmr_prefetch_k"])
                else:
                    prefetch_k0 = int(
                        query.similarity_top_k
                        * kwargs.get("mmr_prefetch_factor", DEFAULT_MMR_PREFETCH_FACTOR)
                    )
            prefetch_k = max(prefetch_k0, query.similarity_top_k)
            #
            prefetch_matches = list(
                self.vector_table.metric_ann_search(
                    vector=query_embedding,
                    n=prefetch_k,
                    metric="cos",
                    metric_threshold=None,  # this is not `mmr_threshold`
                    metadata=query_metadata,
                )
            )
            #
            mmr_threshold = query.mmr_threshold or kwargs.get("mmr_threshold")
            if prefetch_matches:
                pf_match_indices, pf_match_embeddings = zip(
                    *enumerate(match["vector"] for match in prefetch_matches)
                )
            else:
                pf_match_indices, pf_match_embeddings = [], []
            pf_match_indices = list(pf_match_indices)
            pf_match_embeddings = list(pf_match_embeddings)
            mmr_similarities, mmr_indices = get_top_k_mmr_embeddings(
                query_embedding,
                pf_match_embeddings,
                similarity_top_k=query.similarity_top_k,
                embedding_ids=pf_match_indices,
                mmr_threshold=mmr_threshold,
            )
            #
            matches = [prefetch_matches[mmr_index] for mmr_index in mmr_indices]
            top_k_scores = mmr_similarities

        top_k_nodes = []
        top_k_ids = []
        for match in matches:
            node = metadata_dict_to_node(match["metadata"])
            node.set_content(match["body_blob"])
            top_k_nodes.append(node)
            top_k_ids.append(match["row_id"])

        return VectorStoreQueryResult(
            nodes=top_k_nodes,
            similarities=top_k_scores,
            ids=top_k_ids,
        )
