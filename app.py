from sanic import Sanic
from sanic import response
from sanic.exceptions import NotFound
from sanic.views import HTTPMethodView
from sanic_jinja2 import SanicJinja2
import sqlite3
from pathlib import Path
from functools import wraps
import urllib.parse
import json
import hashlib
import sys

app_root = Path(__file__).parent

BUILD_METADATA = 'build-metadata.json'
DB_GLOBS = ('*.db', '*.sqlite', '*.sqlite3')
HASH_BLOCK_SIZE = 1024 * 1024

conns = {}


app = Sanic(__name__)
jinja = SanicJinja2(app)


def get_conn(name):
    if name not in conns:
        info = ensure_build_metadata()[name]
        conns[name] = sqlite3.connect(
            'file:{}?immutable=1'.format(info['file']),
            uri=True
        )
        conns[name].row_factory = sqlite3.Row
    return conns[name]


def ensure_build_metadata(regenerate=False):
    build_metadata = app_root / BUILD_METADATA
    if build_metadata.exists() and not regenerate:
        json.loads(build_metadata.read_text())
    metadata = {}
    for glob in DB_GLOBS:
        for path in app_root.glob(glob):
            name = path.stem
            if name in metadata:
                raise Exception('Multiple files with same stem %s' % name)
            # Calculate hash, efficiently
            m = hashlib.sha256()
            with path.open('rb') as fp:
                while True:
                    data = fp.read(HASH_BLOCK_SIZE)
                    if not data:
                        break
                    m.update(data)
            # List tables and their row counts
            tables = {}
            with sqlite3.connect('file:{}?immutable=1'.format(path.name), uri=True) as conn:
                conn.row_factory = sqlite3.Row
                table_names = [
                    r['name']
                    for r in conn.execute('select * from sqlite_master where type="table"')
                ]
                for table in table_names:
                    tables[table] = conn.execute('select count(*) from "{}"'.format(table)).fetchone()[0]

            metadata[name] = {
                'hash': m.hexdigest(),
                'file': path.name,
                'tables': tables,
            }
    build_metadata.write_text(json.dumps(metadata, indent=4))
    return metadata


class BaseView(HTTPMethodView):
    template = None

    async def get(self, request, db_name, **kwargs):
        name, hash, should_redirect = resolve_db_name(db_name, **kwargs)
        if should_redirect:
            r = response.redirect(should_redirect)
            r.headers['Link'] = '<{}>; rel=preload'.format(
                should_redirect
            )
            return r
        try:
            as_json = kwargs.pop('as_json')
        except KeyError:
            as_json = False
        try:
            data = self.data(request, name, hash, **kwargs)
        except sqlite3.OperationalError as e:
            data = {
                'ok': False,
                'error': str(e),
            }
        if as_json:
            r = response.json(data)
            r.headers['Access-Control-Allow-Origin'] = '*'
        else:
            r = jinja.render(
                self.template,
                request,
                **data,
            )
        # Set far-future cache expiry
        r.headers['Cache-Control'] = 'max-age={}'.format(
            365 * 24 * 60 * 60
        )
        return r


@app.route('/')
async def index(request, sql=None):
    databases = ensure_build_metadata(True)
    return jinja.render(
        'index.html',
        request,
        databases=databases,
    )


@app.route('/favicon.ico')
async def favicon(request):
    return response.text('')


class DatabaseView(BaseView):
    template = 'database.html'

    def data(self, request, name, hash):
        conn = get_conn(name)
        sql = request.args.get('sql') or 'select * from sqlite_master'
        rows = conn.execute(sql)
        columns = [r[0] for r in rows.description]
        return {
            'database': name,
            'database_hash': hash,
            'rows': rows,
            'columns': columns,
        }


class TableView(BaseView):
    template = 'table.html'

    def data(self, request, name, hash, table):
        conn = get_conn(name)
        rows = conn.execute('select * from {} limit 20'.format(table))
        columns = [r[0] for r in rows.description]
        return {
            'database': name,
            'database_hash': hash,
            'table': table,
            'rows': rows,
            'columns': columns,
        }


class RowView(BaseView):
    template = 'table.html'

    def data(self, request, name, hash, table, pk_path):
        conn = get_conn(name)
        pk_values = compound_pks_from_path(pk_path)
        pks = pks_for_table(conn, table)
        wheres = [
            '{}=?'.format(pk)
            for pk in pks
        ]
        sql = 'select * from "{}" where {}'.format(
            table, ' AND '.join(wheres)
        )
        rows = conn.execute(sql, pk_values)
        columns = [r[0] for r in rows.description]
        rows = list(rows)
        if not rows:
            raise NotFound('Record not found: {}'.format(pk_values))
        return {
            'database': name,
            'database_hash': hash,
            'table': table,
            'rows': rows,
            'columns': columns,
        }


app.add_route(DatabaseView.as_view(), '/<db_name:[^/]+?><as_json:(.json)?$>')
app.add_route(TableView.as_view(), '/<db_name:[^/]+>/<table:[^/]+?><as_json:(.json)?$>')
app.add_route(RowView.as_view(), '/<db_name:[^/]+>/<table:[^/]+?>/<pk_path:[^/]+?><as_json:(.json)?$>')


def resolve_db_name(db_name, **kwargs):
    databases = ensure_build_metadata()
    hash = None
    name = None
    if '-' in db_name:
        # Might be name-and-hash, or might just be
        # a name with a hyphen in it
        name, hash = db_name.rsplit('-', 1)
        if name not in databases:
            # Try the whole name
            name = db_name
            hash = None
    else:
        name = db_name
    # Verify the hash
    try:
        info = databases[name]
    except KeyError:
        raise NotFound('Database not found: {}'.format(name))
    expected = info['hash'][:7]
    if expected != hash:
        should_redirect = '/{}-{}'.format(
            name, expected,
        )
        if 'table' in kwargs:
            should_redirect += '/' + kwargs['table']
        return name, expected, should_redirect
    return name, expected, None


def compound_pks_from_path(path):
    return [
        urllib.parse.unquote_plus(b) for b in path.split(',')
    ]


def pks_for_table(conn, table):
    rows = [
        row for row in conn.execute(
            'PRAGMA table_info("{}")'.format(table)
        ).fetchall()
        if row[-1]
    ]
    rows.sort(key=lambda row: row[-1])
    return [r[1] for r in rows]


def path_from_row_pks(row, pks):
    bits = []
    for pk in pks:
        bits.append(urllib.parse.quote_plus(row[pk]))
    return ','.join(bits)


if __name__ == '__main__':
    if '--build' in sys.argv:
        ensure_build_metadata(True)
    else:
        app.run(host="0.0.0.0", port=8006)