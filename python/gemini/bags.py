import logging
import os
from uuid import uuid4

from ast2vec.engine import create_engine
from ast2vec.repo2 import wmhash
from ast2vec.repo2.base import UastExtractor, Transformer, Cacher, UastDeserializer
from pyspark.sql.types import Row


class CassandraSaver(Transformer):
    def __init__(self, keyspace, table="hashes", **kwargs):
        super().__init__(**kwargs)
        self.keyspace = keyspace
        self.table = table

    def __call__(self, head):
        head \
            .flatMap(self.explode) \
            .toDF() \
            .write \
            .format("org.apache.spark.sql.cassandra") \
            .mode("append") \
            .options(table=self.table, keyspace=self.keyspace) \
            .save()

    def explode(self, record):
        key = record[0]
        for col, val in record[1].items():
            yield Row(sha1=key, item=col, value=float(val))


def source2bags(args):
    log = logging.getLogger("source2bags")
    if os.path.exists(args.output):
        log.critical("%s must not exist", args.output)
        return 1
    if not args.config:
        args.config = []
    cas_host, cas_port = args.cassandra.split(":")
    args.config.append("spark.cassandra.connection.host=" + cas_host)
    args.config.append("spark.cassandra.connection.port=" + cas_port)
    engine = create_engine("source2bags-%s" % uuid4(), args.repositories, args)
    log.info("docfreq phase")
    extractors = [wmhash.__extractors__[s](args.min_docfreq) for s in args.feature]
    pipeline = UastExtractor(engine, languages=[args.language])
    if args.persist is not None:
        uasts = pipeline.link(Cacher(args.persist))
    else:
        uasts = pipeline
    uasts = uasts.link(UastDeserializer())
    repo2docfreq = wmhash.Repo2DocFreq(extractors)
    uasts.link(repo2docfreq)
    pipeline.execute()
    log.info("bag + batch phase")
    uasts.unlink(repo2docfreq)
    bags = uasts.link(wmhash.Repo2WeightedSet(extractors))
    if args.persist is not None:
        bags = bags.link(Cacher(args.persist))
    batcher = wmhash.BagsBatcher(extractors)
    bags.link(batcher).link(wmhash.BagsBatchSaver(args.output))
    pipeline.execute()
    log.info("cassandra hashes phase")
    bags.unlink(batcher)
    bags.link(CassandraSaver("gemini"))
    pipeline.execute()
