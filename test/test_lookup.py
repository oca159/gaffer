# -*- coding: utf-8 -
#
# This file is part of gaffer. See the NOTICE for more information.
import copy
import os

import pytest
import pyuv

# patch tornado IOLoop
from gaffer.tornado_pyuv import IOLoop, install
install()


from gaffer.gafferd.http import HttpHandler
from gaffer.gafferd.lookup import LookupClient
from gaffer.lookupd.http import http_server
from gaffer.lookupd.registry import (RemoteJob, GafferNode, Registry,
        NoIdent, JobNotFound, AlreadyIdentified, IdentExists,
        AlreadyRegistered)
from gaffer.loop import patch_loop
from gaffer.manager import Manager
from gaffer.process import ProcessConfig
from gaffer.util import bind_sockets

from test_manager import dummy_cmd

TEST_GAFFERD_HOST = '127.0.0.1'
TEST_GAFFERD_PORT = (os.getpid() % 31000) + 1024
GAFFERD_ADDR = "%s:%s" % (TEST_GAFFERD_HOST, TEST_GAFFERD_PORT)

TEST_LOOKUPD_HOST = '127.0.0.1'
TEST_LOOKUPD_PORT = (os.getpid() % 31000) + 1023
LOOKUPD_ADDR = "%s:%s" % (TEST_LOOKUPD_HOST, TEST_LOOKUPD_PORT)

def test_registry_basic():
    r = Registry()
    # fake connections
    c1 = object()
    c2 = object()
    r.add_node(c1)
    r.add_node(c2)

    assert c1 in r.nodes
    assert c2 in r.nodes

    assert isinstance(r.nodes[c1], GafferNode)
    assert isinstance(r.nodes[c2], GafferNode)
    assert r.nodes[c1] is not r.nodes[c2]

    r.remove_node(c2)
    assert c2 not in r.nodes

def test_registry_identify():
    r = Registry()
    c1 = object()
    c2 = object()
    c3 = object()
    r.add_node(c1)
    r.add_node(c2)
    r.add_node(c3)

    with pytest.raises(NoIdent):
        r.get_node(c1)

    updated = r.nodes[c1].updated
    r.identify(c1, "c1", 8000, "broadcast", 1.0)

    n1 = r.get_node(c1)
    assert updated != n1.updated

    assert n1.hostname == "c1"
    assert n1.port == 8000
    assert n1.broadcast_address == "broadcast"
    assert n1.version == 1.0

    with pytest.raises(AlreadyIdentified):
        r.identify(c1, "c1", 8000, "broadcast", 1.0)

    with pytest.raises(IdentExists):
        r.identify(c2, "c1", 8000, "broadcast", 1.0)

    r.identify(c2, "c1", 8001, "broadcast", 1.0)
    n2 = r.get_node(c2)

    assert n2.hostname == "c1"
    assert n2.port == 8001

    r.identify(c3, "c3", 8000, "broadcast", 1.0)
    n3 = r.get_node(c3)

    assert n3.hostname == "c3"
    assert n3.port == 8000

def test_registry_add_job():
    r = Registry()
    c1 = object()
    c2 = object()
    c3 = object()
    c4 = object()
    r.add_node(c1)
    r.identify(c1, "c1", 8000, "broadcast", 1.0)
    r.add_node(c2)
    r.identify(c2, "c2", 8000, "broadcast2", 1.0)
    r.add_node(c3)
    r.identify(c3, "c3", 8000, "broadcast2", 1.0)
    r.add_node(c4)
    r.identify(c4, "c4", 8000, "broadcast2", 1.0)

    n1 = r.get_node(c1)
    n2 = r.get_node(c2)
    n3 = r.get_node(c3)
    n4 = r.get_node(c4)

    assert r.sessions() == {}
    assert len(r.jobs()) == 0

    r.add_job(c1, 'a.job1')
    sessions = r.sessions()
    assert 'a' in sessions
    assert 'a.job1' in sessions['a']
    assert len(sessions['a']['a.job1']) == 1
    job = sessions['a']['a.job1'][0]
    assert isinstance(job, RemoteJob)
    assert job.name == 'a.job1'
    assert job.node is n1
    assert len(r.jobs()) == 1
    assert job == r.find_job('a.job1')[0]

    jobs = r.jobs()
    assert list(jobs) == ['a.job1']
    assert jobs['a.job1'][0] == job
    assert len(n1.sessions) == 1

    with pytest.raises(AlreadyRegistered):
        r.add_job(c1, 'a.job1')

    r.add_job(c2, 'a.job1')
    sessions = r.sessions()
    assert len(sessions['a']['a.job1']) == 2
    job = sessions['a']['a.job1'][1]
    assert isinstance(job, RemoteJob)
    assert job.name == 'a.job1'
    assert job.node is n2
    assert len(r.jobs()) == 1
    jobs = r.jobs()
    assert list(jobs) == ['a.job1']
    assert jobs['a.job1'][1] == job

    r.add_job(c3, 'a.job2')
    sessions = r.sessions()
    assert len(sessions['a']['a.job1']) == 2
    assert len(sessions['a']['a.job2']) == 1
    job = sessions['a']['a.job2'][0]
    assert isinstance(job, RemoteJob)
    assert job.name == 'a.job2'
    assert job.node is n3
    assert len(r.jobs()) == 2
    jobs = r.jobs()
    assert list(jobs) == ['a.job1', 'a.job2']
    assert jobs['a.job2'][0] == job

    r.add_job(c4, 'b.job1')
    sessions = r.sessions()
    assert len(sessions) == 2
    assert len(sessions['b']['b.job1']) == 1
    job = sessions['b']['b.job1'][0]
    assert isinstance(job, RemoteJob)
    assert job.name == 'b.job1'
    assert job.node is n4
    assert len(r.jobs()) == 3
    jobs = r.jobs()
    assert list(jobs) == ['a.job1', 'a.job2', 'b.job1']

    assert jobs['a.job1'][0].node == n1
    assert jobs['a.job1'][1].node == n2
    assert jobs['a.job2'][0].node == n3
    assert jobs['b.job1'][0].node == n4
    return r, c1, c2, c3, c3, n1, n2, n3, n4

def test_registry_remove_job():
    r, c1, c2, c3, c3, n1, n2, n3, n4 = test_registry_add_job()

    # remove a job
    assert len(r.sessions()['a']['a.job1']) == 2
    assert len(n2.sessions['a']) == 1
    r.remove_job(c2, 'a.job1')
    assert 'a' not in n2.sessions
    assert len(r.sessions()['a']['a.job1']) == 1
    assert list(r.jobs()) == ['a.job1', 'a.job2', 'b.job1']


    r.remove_job(c1, 'a.job1')
    assert 'a.job1' not in r.sessions()['a']
    assert list(r.jobs()) == ['a.job2', 'b.job1']

    with pytest.raises(JobNotFound):
        r.find_job('a.job1')

def test_registry_add_process():
    r = Registry()
    c1 = object()
    r.add_node(c1)

    with pytest.raises(NoIdent):
        r.add_process(c1, "a.job1", 1)

    r.identify(c1, "c1", 8000, "broadcast", 1.0)

    with pytest.raises(JobNotFound):
        r.add_process(c1, "a.job1", 1)

    r.add_job(c1, "a.job1")
    r.add_process(c1, "a.job1", 1)
    job = r.find_job("a.job1")[0]
    assert job.pids == [1]
    assert job.node == r.get_node(c1)

    c2 = object()
    r.add_node(c2)
    r.identify(c2, "c2", 8000, "broadcast", 1.0)
    r.add_job(c2, "a.job1")
    r.add_process(c2, "a.job1", 1)

    jobs = r.find_job("a.job1")
    assert len(jobs) == 2

    job2 = r.find_job("a.job1")[1]
    assert job2.pids == [1]
    assert job2.node == r.get_node(c2)
    assert job2 != job

    r.add_process(c1, "a.job1", 2)
    job = r.find_job("a.job1")[0]
    assert job.pids == [1, 2]
    jobs = r.find_job("a.job1")
    assert len(jobs) == 2

    return r, c1, c2

def test_registry_remove_process():
    r, c1, c2 = test_registry_add_process()

    r.remove_process(c1, "a.job1", 1)
    job = r.find_job("a.job1")[0]
    assert job.pids == [2]

    # just to confirm we don't raise anything
    r.remove_process(c1, "a.job1", 1)

    r.remove_process(c1, "a.job1", 2)
    assert job.pids == []

    assert len(r.find_job("a.job1")) == 2
    job2 = r.find_job("a.job1")[1]
    assert job2.pids == [1]


def test_registry_events():
    loop = pyuv.Loop.default_loop()
    async = pyuv.Async(loop, lambda h: h.stop())

    r = Registry(loop)
    emitted = []
    def cb(event, message):
        emitted.append((event, copy.deepcopy(message)))

    r.bind_all(cb)
    c1 = object()
    r.add_node(c1)

    r.identify(c1, "c1", 8000, "broadcast", 1.0)
    r.update(c1)
    r.add_job(c1, "a.job1")
    r.add_process(c1, "a.job1", 1)
    r.remove_process(c1, "a.job1", 1)
    r.remove_job(c1, "a.job1")
    r.remove_node(c1)

    t = pyuv.Timer(loop)
    t.start(lambda h: async.close(), 0.2, 0.0)
    loop.run()

    assert len(emitted) == 7
    actions = [line[0] for line in emitted]
    assert list(actions) == ['add_node', 'identify', 'add_job', 'add_process',
            'remove_process', 'remove_job', 'remove_node']

    assert isinstance(emitted[0][1], GafferNode)
    assert isinstance(emitted[1][1], GafferNode)
    assert isinstance(emitted[2][1], dict)
    assert "job_name" in emitted[2][1]
    assert emitted[2][1]['job_name'] == "a.job1"
    assert isinstance(emitted[3][1], dict)
    assert "job_name" in emitted[3][1]
    assert emitted[3][1]['job_name'] == "a.job1"
    assert "pid" in emitted[3][1]
    assert emitted[3][1]['pid'] == 1
    assert isinstance(emitted[4][1], dict)
    assert "job_name" in emitted[4][1]
    assert emitted[4][1]['job_name'] == "a.job1"
    assert "pid" in emitted[4][1]
    assert emitted[4][1]['pid'] == 1
    assert isinstance(emitted[5][1], dict)
    assert emitted[5][1]['job_name'] == "a.job1"
    assert isinstance(emitted[6][1], GafferNode)
    assert emitted[6][1].sessions == {}

def test_lookup_service():
    loop = pyuv.Loop.default_loop()
    r = Registry(loop)
    sock = bind_sockets(LOOKUPD_ADDR)
    io_loop = IOLoop(_loop=loop)
    server = http_server(io_loop, sock, registration_db=r)
    server.start()

    emitted = []
    def cb(event, message):
        emitted.append((event, message))

    r.bind_all(cb)

    client = LookupClient(loop, "ws://%s/ws" % LOOKUPD_ADDR)
    client.start()

    messages = []
    messages.append(client.identify("c1", 8000, "broadcast", 1.0))
    messages.append(client.ping())
    messages.append(client.add_job("a.job1"))
    messages.append(client.add_process("a.job1", 1))
    messages.append(client.remove_process("a.job1", 1))
    messages.append(client.remove_job("a.job1"))

    t0 = pyuv.Timer(loop)
    t0.start(lambda h: client.close(), 0.4, 0.0)


    def stop(h):
        h.close()
        server.stop()
        io_loop.close(True)

    t = pyuv.Timer(loop)
    t.start(stop, 0.6, 0.0)
    loop.run()

    assert len(messages) == 6
    results = ["ok" in msg.result() for msg in messages]
    assert results == [True, True, True, True, True, True]

    assert len(emitted) == 7
    actions = [line[0] for line in emitted]
    assert list(actions) == ['add_node', 'identify', 'add_job', 'add_process',
            'remove_process', 'remove_job', 'remove_node']

    assert isinstance(emitted[0][1], GafferNode)
    assert isinstance(emitted[1][1], GafferNode)
    assert isinstance(emitted[2][1], dict)
    assert "job_name" in emitted[2][1]
    assert emitted[2][1]['job_name'] == "a.job1"
    assert isinstance(emitted[3][1], dict)
    assert "job_name" in emitted[3][1]
    assert emitted[3][1]['job_name'] == "a.job1"
    assert "pid" in emitted[3][1]
    assert emitted[3][1]['pid'] == 1
    assert isinstance(emitted[4][1], dict)
    assert "job_name" in emitted[4][1]
    assert emitted[4][1]['job_name'] == "a.job1"
    assert "pid" in emitted[4][1]
    assert emitted[4][1]['pid'] == 1
    assert isinstance(emitted[5][1], dict)
    assert emitted[5][1]['job_name'] == "a.job1"
    assert isinstance(emitted[6][1], GafferNode)
    assert emitted[6][1].sessions == {}

def test_lookup_manager():

    # intiallize the lookupd server
    loop = pyuv.Loop.default_loop()
    r = Registry(loop)
    sock = bind_sockets(LOOKUPD_ADDR)
    io_loop = IOLoop(_loop=loop)
    server = http_server(io_loop, sock, registration_db=r)
    server.start()

    # subscribe to events
    emitted = []
    def cb(event, message):
        emitted.append((event, message))
    r.bind_all(cb)


    # start the manager with the HTTP API
    http_handler = HttpHandler(uri=GAFFERD_ADDR,
            lookupd_addresses=["http://%s" % LOOKUPD_ADDR])
    m = Manager(loop=loop)
    m.start(apps=[http_handler])

    testfile, cmd, args, wdir = dummy_cmd()
    config = ProcessConfig("dummy", cmd, args=args, cwd=wdir)
    m.load(config)
    m.stop_process(1)
    m.unload("dummy")

    t = pyuv.Timer(loop)

    def do_stop(h):
        server.stop()
        io_loop.close(True)

    def stop_server(m):
        t.start(do_stop, 0.4, 0.0)

    m.stop(stop_server)
    loop.run()

    assert len(emitted) == 7
    actions = [line[0] for line in emitted]
    assert list(actions) == ['add_node', 'identify', 'add_job', 'add_process',
            'remove_process', 'remove_job', 'remove_node']


    assert isinstance(emitted[0][1], GafferNode)
    assert isinstance(emitted[1][1], GafferNode)
    assert isinstance(emitted[2][1], dict)
    assert "job_name" in emitted[2][1]
    assert emitted[2][1]['job_name'] == "default.dummy"
    assert isinstance(emitted[3][1], dict)
    assert "job_name" in emitted[3][1]
    assert emitted[3][1]['job_name'] == "default.dummy"
    assert "pid" in emitted[3][1]
    assert emitted[3][1]['pid'] == 1
    assert isinstance(emitted[4][1], dict)
    assert "job_name" in emitted[4][1]
    assert emitted[4][1]['job_name'] == "default.dummy"
    assert "pid" in emitted[4][1]
    assert emitted[4][1]['pid'] == 1
    assert isinstance(emitted[5][1], dict)
    assert emitted[5][1]['job_name'] == "default.dummy"
    assert isinstance(emitted[6][1], GafferNode)
    assert emitted[6][1].sessions == {}

if __name__ == "__main__":
    test_lookup_manager()