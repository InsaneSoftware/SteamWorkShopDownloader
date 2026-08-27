import logging

from waitress import serve

from workshop_porter import create_app
from workshop_porter.config import Config


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_file()
    application = create_app(config)
    serve(
        application,
        host=config.host,
        port=config.port,
        threads=config.threads,
        connection_limit=config.connection_limit,
        channel_timeout=config.channel_timeout,
        ident="Workshop Porter",
    )
