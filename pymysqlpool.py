"""
author: ljk
email: chaoyuemyself@hotmail.com
"""
import pymysql
import warnings
import queue
import logging
import threading


__all__ = ['Connection', 'ConnectionPool', 'logger']


logger = logging.getLogger(__name__)


class Connection(pymysql.connections.Connection):
    """
    Return a connection object with or without connection_pool feature.
    This is all the same with pymysql.connections.Connection instance except that with connection_pool feature:
        the __exit__() method additionally put the connection back to it's pool
    """
    _pool = None
    _reusable_expection = (pymysql.err.ProgrammingError, pymysql.err.IntegrityError, pymysql.err.NotSupportedError)

    def __init__(self, *args, **kwargs):
        pymysql.connections.Connection.__init__(self, *args, **kwargs)
        self.args = args
        self.kwargs = kwargs

    def __exit__(self, exc, value, traceback):
        """
        Overwrite the __exit__() method of pymysql.connections.Connection
        Base action: on successful exit, commit. On exception, rollback
        With pool additional action: put connection back to pool
        """
        pymysql.connections.Connection.__exit__(self, exc, value, traceback)
        if self._pool:
            if not exc or exc in self._reusable_expection:
                '''reusable connection'''
                self._pool.put_connection(self)
            else:
                '''no reusable connection, close it and create a new one then put it to the pool'''
                self._pool.put_connection(self._recreate(*self.args, **self.kwargs))
                self._pool = None
                try:
                    self.close()
                    logger.warning("Close not reusable connection from pool(%s) caused by %s", self._pool.name, value)
                except Exception:
                    pass

    def _recreate(self, *args, **kwargs):
        conn = Connection(*args, **kwargs)
        logger.debug('Create new connection due to pool(%s) lacking', self._pool.name)
        return conn

    def close(self):
        """
        Overwrite the close() method of pymysql.connections.Connection
        With pool, put connection back to pool;
        Without pool, send the quit message and close the socket
        """
        if self._pool:
            self._pool.put_connection(self)
        else:
            pymysql.connections.Connection.close(self)

    def execute_query(self, query, args=(), dictcursor=False, return_one=False, exec_many=False):
        """
        A wrapped method of pymysql's execute() or executemany().
        dictcursor: whether want use the dict cursor(cursor's default type is tuple)
        return_one: whether want only one row of the result
        exec_many: whether use pymysql's executemany() method
        """
        with self:
            cur = self.cursor() if not dictcursor else self.cursor(pymysql.cursors.DictCursor)
            try:
                if exec_many:
                    cur.executemany(query, args)
                else:
                    cur.execute(query, args)
            except Exception:
                raise
            # if no record match the query, return () if return_one==False, else return None
            return cur.fetchone() if return_one else cur.fetchall()


class ConnectionPool:
    """
    Return connection_pool object, which has method can get connection from a pool with timeout and retry feature;
    put a reusable connection back to the pool, etc; also we can create different instance of this class that represent
    different pool of different DB Server or different user
    """
    _HARD_LIMIT = 200
    _THREAD_LOCAL = threading.local()
    _THREAD_LOCAL.retry_counter = 0  # a counter used for debug get_connection() method

    def __init__(self, size=5, name=None, *args, **kwargs):
        self._pool = queue.Queue(self._HARD_LIMIT)
        self._size = size if 0 < size < self._HARD_LIMIT else self._HARD_LIMIT
        self.name = name if name else '-'.join(
            [kwargs.get('host', 'localhost'), str(kwargs.get('port', 3306)),
             kwargs.get('user', ''), kwargs.get('database', '')])
        for _ in range(self._size):
            conn = Connection(*args, **kwargs)
            conn._pool = self
            self._pool.put(conn)

    def get_connection(self, timeout=1, retry_num=1):
        """
        timeout: timeout of get a connection from pool, should be a int(0 means return or raise immediately)
        retry_num: how many times will retry to get a connection
        """
        try:
            conn = self._pool.get(timeout=timeout) if timeout > 0 else self._pool.get_nowait()
            logger.debug('Get connection from pool(%s)', self.name)
            return conn
        except queue.Empty:
            if not hasattr(self._THREAD_LOCAL, 'retry_counter'):
                self._THREAD_LOCAL.retry_counter = 0
            if retry_num > 0:
                self._THREAD_LOCAL.retry_counter += 1
                logger.debug('Retry get connection from pool(%s), the %d times', self.name, self._THREAD_LOCAL.retry_counter)
                retry_num -= 1
                return self.get_connection(timeout, retry_num)
            else:
                total_times = self._THREAD_LOCAL.retry_counter + 1
                self._THREAD_LOCAL.retry_counter = 0
                raise GetConnectionFromPoolError("can't get connection from pool({}) within {}*{} second(s)".format(
                    self.name, timeout, total_times))

    def put_connection(self, conn):
        if not conn._pool:
            conn._pool = self
        conn.cursor().close()
        try:
            self._pool.put_nowait(conn)
            logger.debug("Put connection back to pool(%s)", self.name)
        except queue.Full:
            logger.warning("Put connection to pool(%s) error, pool is full, size:%d", self.name, self.size())

    def size(self):
        return self._pool.qsize()


class GetConnectionFromPoolError(Exception):
    """Exception related can't get connection from pool within timeout seconds."""
