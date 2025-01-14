""" ExternalConfigServer is a replacement for WSGITileServer that uses external
    configuration fetched via HTTP to service all config requests.
    
    Example usage, with gunicorn (http://gunicorn.org):
      
      gunicorn --bind localhost:8888 "TileStache.Goodies.ExternalConfigServer:WSGIServer(url)"
"""

from urllib.request import urlopen
import logging
import psycopg2
import json
import os
import dotenv

try:
    from json import load as json_load
except ImportError:
    from simplejson import load as json_load

import TileStache

logger = logging.getLogger(__name__)

class DBLayers:

    def query_db(self, query, results='all'):
        cursor = self.connection.cursor()
        cursor.execute(query)
        if results == 'all':
            result = cursor.fetchall()
        elif results == 'one':
            result = cursor.fetchone()
        cursor.close()
        return result

    def __init__(self, config):
        self.connection = config.db_connection
        self.seen_layers = {}
        self.config = config
        for key in list(self.keys()):
            self.fetch_layer_from_db(key)

    def keys(self):
        # return a list of key strings
        key_results = self.query_db("SELECT key FROM tilestache_layer;")
        return [k[0] for k in key_results]

    def items(self):
        # return a list of (key, layer) tuples
        return self.query_db("SELECT key, value FROM tilestache_layer;")

    def __contains__(self, key):
        if key in self.seen_layers:
            return True
        else:
            result = self.query_db("SELECT COUNT(key) FROM tilestache_layer WHERE key='{0}';".format(key), results='one')
            # return True if the key is here
            return result[0] == True

    def fetch_layer_from_db(self, key):
        raw_result = self.query_db("SELECT value, updated FROM tilestache_layer WHERE key='{0}';".format(key))[0]
        layer_dict = json.loads(raw_result[0])
        layer = TileStache.Config._parseConfigLayer(layer_dict, self.config, '/tmp/stache')
        layer.key = key
        self.seen_layers[key] = dict(layer=layer, updated=raw_result[1])
        return layer

    def check_style_status(self, key):
        """
        if the layer has been updated within the last minute
        remove the layer.provider.mapnik property to force TileStache to re-read the style
        """
        updated = self.query_db("SELECT updated from tilestache_layer where key='{0}';".format(key))[0][0]
        last_read = self.seen_layers[key]['updated']
        if updated != last_read:
            self.seen_layers[key]['updated'] = updated
            return True
        else:
            return False

    def __getitem__(self, key):
        # return the layer named by the key
        if key in self.seen_layers:
            layer = self.seen_layers.get(key, 'not found')['layer']
            if not layer:
                del self.seen_layers[key]
            else:
                stale = self.check_style_status(key)
                if stale:
                    logger.info("layer is stale - rereading mapnik style")
                    layer.provider.mapnik = None
                    return self.fetch_layer_from_db(key)

                return layer
        return self.fetch_layer_from_db(key)


class PGConfiguration:

    def __init__(self, connection, dirpath, loglevel, config_name='default'):
        self.db_connection = connection

        cache_dict = self.get_cache_dict(config_name)
        if loglevel not in cache_dict:
            cache_dict['loglevel'] = loglevel
        path = cache_dict.get('path', dirpath)
        self.cache = TileStache.Config._parseConfigCache(cache_dict, path)
        self.dirpath = path
        self.layers = DBLayers(self)

    def get_cache_dict(self, config_name):
        cur = self.db_connection.cursor()
        cur.execute("SELECT cache FROM tilestache_config WHERE name='{0}'".format(config_name))
        config_singleton = json.loads(cur.fetchone()[0])
        cur.close()
        return config_singleton


class WSGIServer(TileStache.WSGITileServer):

    """
    Wrap WSGI application, passing it a custom configuration.

    The WSGI application is an instance of TileStache:WSGITileServer.

    This method is initiated with a url_root that contains the scheme, host, port
    and path that must prefix the API calls on our local server.  Any valid http
    or https urls should work.

    The cache_responses parameter tells TileStache to cache all responses from
    the configuration server.
    """

    def __init__(self, dotenv_file, debug_level="INFO"):
        logging.basicConfig(level=debug_level)

        dotenv.read_dotenv(dotenv_file)
        db_connection_dict = {
            'database': os.environ['TILESTACHE_DATABASE_NAME'],
            'user':     os.environ['TILESTACHE_DATABASE_USERNAME'],
            'password': os.environ['TILESTACHE_DATABASE_PASSWORD'],
            'host':     os.environ['TILESTACHE_DATABASE_HOST'],
            'port':     os.environ['TILESTACHE_DATABASE_PORT'],
        }

        dirpath = os.environ['TILESTACHE_TMP_DIR']

        connection = psycopg2.connect(**db_connection_dict)
        config = PGConfiguration(connection, dirpath, debug_level)
        TileStache.WSGITileServer.__init__(self, config, False)

    def __call__(self, environ, start_response):
        response = TileStache.WSGITileServer.__call__(self, environ, start_response)
        return response
