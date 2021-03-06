# @author Jason Chin
#
# Copyright (C) 2010 by Jason Chin 
# Copyright (C) 2011 by Jason Chin, Pacific Biosciences
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""

PypeController: This module provides the PypeWorkflow that controlls how a workflow is excuted.

"""
import sys
import datetime
import multiprocessing
import threading 
import time 
import traceback
import logging
import Queue
from cStringIO import StringIO 
from urlparse import urlparse

# TODO(CD): When we stop using Python 2.5, use relative-imports and remove this dir from PYTHONPATH.
from common import PypeError, PypeObject, Graph, URIRef, pypeNS
from data import PypeDataObjectBase, PypeSplittableLocalFile
from task import PypeTaskBase, PypeTaskCollection, PypeThreadTaskBase, getFOFNMapTasks
from task import TaskInitialized, TaskDone, TaskFail

logger = logging.getLogger(__name__)

class TaskExecutionError(PypeError):
    pass
class TaskTypeError(PypeError):
    pass
class TaskFailureError(PypeError):
    pass
class LateTaskFailureError(PypeError):
    pass

class PypeNode(object):
    """ 
    Representing a node in the dependence DAG. 
    """

    def __init__(self, obj):
        self.obj = obj
        self._outNodes = set()
        self._inNodes = set()

    def addAnOutNode(self, obj):
        self._outNodes.add(obj)
        
    def addAnInNode(self, obj):
        self._inNodes.add(obj)

    def removeAnOutNode(self, obj):
        self._outNodes.remove(obj)

    def removeAnInNode(self, obj):
        self._inNodes.remove(obj)

    @property
    def inDegree(self):
        return len(self._inNodes)

    @property
    def outDegree(self):
        return len(self._outNodes)
    
    @property
    def depth(self):
        if self.inDegree == 0:
            return 1
        return 1 + max([ node.depth for node in self._inNodes ])

class PypeGraph(object):
    """ 
    Representing a dependence DAG with PypeObjects. 
    """

    def __init__(self, RDFGraph, subGraphNodes=None):
        """
        Construct an internal DAG with PypeObject given an RDF graph.
        A sub-graph can be constructed if subGraphNodes is not "None"
        """

        self._RDFGraph = RDFGraph
        self._allEdges = set()
        self._allNodes = set()
        self.url2Node ={}

        for row in self._RDFGraph.query('SELECT ?s ?o WHERE {?s pype:prereq ?o . }', initNs=dict(pype=pypeNS)):
            if subGraphNodes != None:
                if row[0] not in subGraphNodes: continue
                if row[1] not in subGraphNodes: continue
            
            sURL, oURL = str(row[0]), str(row[1])
            
            self.url2Node[sURL] = self.url2Node.get( sURL, PypeNode(str(row[0])) )
            self.url2Node[oURL] = self.url2Node.get( oURL, PypeNode(str(row[1])) )

            n1 = self.url2Node[oURL]
            n2 = self.url2Node[sURL]
            
            n1.addAnOutNode(n2)
            n2.addAnInNode(n1)
            
            anEdge = (n1, n2)
            self._allNodes.add( n1 )
            self._allNodes.add( n2 )
            self._allEdges.add( anEdge )
            
    def __getitem__(self, url):
        """PypeGraph["URL"] ==> PypeNode"""
        return self.url2Node[url]

    def tSort(self): #return a topoloical sort node list
        """
        Output topological sorted list of the graph element. 
        It raises a TeskExecutionError if a circle is detected.
        """
        edges = self._allEdges.copy()
        
        S = [x for x in self._allNodes if x.inDegree == 0]
        L = []
        while len(S) != 0:
            n = S.pop()
            L.append(n)
            outNodes = n._outNodes.copy()
            for m in outNodes:
                edges.remove( (n, m) )
                n.removeAnOutNode(m)
                m.removeAnInNode(n)
                if m.inDegree == 0:
                    S.append(m)
        
        if len(edges) != 0:
            raise TaskExecutionError(" Circle detectd in the dependency graph ")
        else:
            return [x.obj for x in L]
                    
class PypeWorkflow(PypeObject):
    """ 
    Representing a PypeWorkflow. PypeTask and PypeDataObjects can be added
    into the workflow and executed through the instanct methods.

    >>> import os, time 
    >>> from pypeflow.data import PypeLocalFile, makePypeLocalFile, fn
    >>> from pypeflow.task import *
    >>> try:
    ...     os.makedirs("/tmp/pypetest")
    ...     _ = os.system("rm -f /tmp/pypetest/*")
    ... except Exception:
    ...     pass
    >>> time.sleep(1)
    >>> fin = makePypeLocalFile("/tmp/pypetest/testfile_in", readOnly=False)
    >>> fout = makePypeLocalFile("/tmp/pypetest/testfile_out", readOnly=False)
    >>> @PypeTask(outputDataObjs={"test_out":fout},
    ...           inputDataObjs={"test_in":fin},
    ...           parameters={"a":'I am "a"'}, **{"b":'I am "b"'})
    ... def test(self):
    ...     print test.test_in.localFileName
    ...     print test.test_out.localFileName
    ...     os.system( "touch %s" % fn(test.test_out) )
    ...     pass
    >>> os.system( "touch %s" %  (fn(fin))  )
    0
    >>> from pypeflow.controller import PypeWorkflow
    >>> wf = PypeWorkflow()
    >>> wf.addTask(test)
    >>> def finalize(self):
    ...     def f():
    ...         print "in finalize:", self._status
    ...     return f
    >>> test.finalize = finalize(test)  # For testing only. Please don't do this in your code. The PypeTask.finalized() is intended to be overriden by subclasses. 
    >>> wf.refreshTargets( objs = [fout] )
    /tmp/pypetest/testfile_in
    /tmp/pypetest/testfile_out
    in finalize: done
    True
    """

    supportedURLScheme = ["workflow"]

    def __init__(self, URL = None, **attributes ):

        if URL == None:
            URL = "workflow://" + __file__+"/%d" % id(self)

        self._pypeObjects = {}

        PypeObject.__init__(self, URL, **attributes)

        self._referenceRDFGraph = None #place holder for a reference RDF

        
    def addObject(self, obj):
        self.addObjects([obj])

    def addObjects(self, objs):
        """
        Add data objects into the workflow. One can add also task object to the workflow using this method for
        non-threaded workflow.
        """
        for obj in objs:
            if obj.URL in self._pypeObjects:
                if id(self._pypeObjects[obj.URL]) != id(obj):
                    raise PypeError, "Add different objects with the same URL %s" % obj.URL
                else:
                    continue
            self._pypeObjects[obj.URL] = obj

    def addTask(self, taskObj):
        self.addTasks([taskObj])


    def addTasks(self, taskObjs):
        """
        Add tasks into the workflow. The dependent input and output data objects are added automatically too. 
        It sets the message queue used for communicating between the task thread and the main thread. One has
        to use addTasks() or addTask() to add task objects to a threaded workflow.
        """
        for taskObj in taskObjs:
            if isinstance(taskObj, PypeTaskCollection):
                for subTaskObj in taskObj.getTasks() + taskObj.getScatterGatherTasks():
                    self.addObjects(subTaskObj.inputDataObjs.values())
                    self.addObjects(subTaskObj.outputDataObjs.values())
                    self.addObjects(subTaskObj.mutableDataObjs.values())
                    self.addObject(subTaskObj)

            else:
                for dObj in taskObj.inputDataObjs.values() +\
                            taskObj.outputDataObjs.values() +\
                            taskObj.mutableDataObjs.values() :
                    if isinstance(dObj, PypeSplittableLocalFile):
                        self.addObjects([dObj._completeFile])
                    self.addObjects([dObj])

                self.addObject(taskObj)

            
    def removeTask(self, taskObj):
        self.removeTasks([taskObj])
        
    def removeTasks(self, taskObjs ):
        """
        Remove tasks from the workflow.
        """
        self.removeObjects(taskObjs)
            
    def removeObjects(self, objs):
        """
        Remove objects from the workflow. If the object cannot be found, a PypeError is raised.
        """
        for obj in objs:
            if obj.URL in self._pypeObjects:
                del self._pypeObjects[obj.URL]
            else:
                raise PypeError, "Unable to remove %s from the graph. (Object not found)" % obj.URL

    def updateURL(self, oldURL, newURL):
        obj = self._pypeObjects[oldURL]
        obj._updateURL(newURL)
        self._pypeObjects[newURL] = obj
        del self._pypeObjects[oldURL]


            
    @property
    def _RDFGraph(self):
        # expensive to recompute
        graph = Graph()
        for URL, obj in self._pypeObjects.iteritems():
            for s,p,o in obj._RDFGraph:
                graph.add( (s,p,o) )
        return graph

    def setReferenceRDFGraph(self, fn):
        self._referenceRDFGraph = Graph()
        self._referenceRDFGraph.load(fn)
        refMD5s = self._referenceRDFGraph.subject_objects(pypeNS["codeMD5digest"])
        for URL, md5digest in refMD5s:
            obj = self._pypeObjects[str(URL)]
            obj.setReferenceMD5(md5digest)

    def _graphvizDot(self, shortName=False):
        graph = self._RDFGraph
        dotStr = StringIO()
        shapeMap = {"file":"box", "state":"box", "task":"component"}
        colorMap = {"file":"yellow", "state":"cyan", "task":"green"}
        dotStr.write( 'digraph "%s" {\n rankdir=LR;' % self.URL)
        for URL in self._pypeObjects.keys():
            URLParseResult = urlparse(URL)
            if URLParseResult.scheme not in shapeMap:
                continue
            else:
                shape = shapeMap[URLParseResult.scheme]
                color = colorMap[URLParseResult.scheme]

                s = URL
                if shortName == True:
                    s = URLParseResult.scheme + "://..." + URLParseResult.path.split("/")[-1] 
                dotStr.write( '"%s" [shape=%s, fillcolor=%s, style=filled];\n' % (s, shape, color))

        for row in graph.query('SELECT ?s ?o WHERE {?s pype:prereq ?o . }', initNs=dict(pype=pypeNS)):
            s, o = row
            if shortName == True:
                    s = urlparse(s).scheme + "://..." + urlparse(s).path.split("/")[-1] 
                    o = urlparse(o).scheme + "://..." + urlparse(o).path.split("/")[-1] 
            dotStr.write( '"%s" -> "%s";\n' % (o, s))
        for row in graph.query('SELECT ?s ?o WHERE {?s pype:hasMutable ?o . }', initNs=dict(pype=pypeNS)):
            s, o = row
            if shortName == True:
                    s = urlparse(s).scheme + "://..." + urlparse(s).path.split("/")[-1] 
                    o = urlparse(o).scheme + "://..." + urlparse(o).path.split("/")[-1] 
            dotStr.write( '"%s" -- "%s" [arrowhead=both, style=dashed ];\n' % (s, o))
        dotStr.write ("}")
        return dotStr.getvalue()

    @property
    def graphvizDot(self):
        return self._graphvizDot()

    @property
    def graphvizShortNameDot(self):
        return self._graphvizDot(shortName = True)

    @property
    def makeFileStr(self):
        """
        generate a string that has the information of the execution dependency in
        a "Makefile" like format. It can be written into a "Makefile" and
        executed by "make".
        """
        for URL in self._pypeObjects.keys():
            URLParseResult = urlparse(URL)
            if URLParseResult.scheme != "task": continue
            taskObj = self._pypeObjects[URL]
            if not hasattr(taskObj, "script"):
                raise TaskTypeError("can not convert non shell script based workflow to a makefile") 
        makeStr = StringIO()
        for URL in self._pypeObjects.keys():
            URLParseResult = urlparse(URL)
            if URLParseResult.scheme != "task": continue
            taskObj = self._pypeObjects[URL]
            inputFiles = taskObj.inputDataObjs
            outputFiles = taskObj.outputDataObjs
            #for oStr in [o.localFileName for o in outputFiles.values()]:
            if 1:
                oStr = " ".join( [o.localFileName for o in outputFiles.values()])

                iStr = " ".join([i.localFileName for i in inputFiles.values()])
                makeStr.write( "%s:%s\n" % ( oStr, iStr ) )
                makeStr.write( "\t%s\n\n" % taskObj.script )
        makeStr.write("all: %s" %  " ".join([o.localFileName for o in outputFiles.values()]) )
        return makeStr.getvalue()

    @staticmethod
    def getSortedURLs(rdfGraph, objs):
        if len(objs) != 0:
            connectedPypeNodes = set()
            for obj in objs:
                if isinstance(obj, PypeSplittableLocalFile):
                    obj = obj._completeFile
                for x in rdfGraph.transitive_objects(URIRef(obj.URL), pypeNS["prereq"]):
                    connectedPypeNodes.add(x)
            tSortedURLs = PypeGraph(rdfGraph, connectedPypeNodes).tSort( )
        else:
            tSortedURLs = PypeGraph(rdfGraph).tSort( )
        return tSortedURLs

    def refreshTargets(self, objs = [], callback = (None, None, None) ):
        """
        Execute the DAG to reach all objects in the "objs" argument.
        """
        tSortedURLs = self.getSortedURLs(self._RDFGraph, objs)
        for URL in tSortedURLs:
            obj = self._pypeObjects[URL]
            if not isinstance(obj, PypeTaskBase):
                continue
            else:
                obj()
                obj.finalize()
        self._runCallback(callback)
        return True

    def _runCallback(self, callback = (None, None, None ) ):
        if callback[0] != None and callable(callback[0]):
            argv = []
            kwargv = {}
            if callback[1] != None and isinstance( callback[1], type(list()) ):
                argv = callback[1]
            else:
                raise TaskExecutionError( "callback argument type error") 

            if callback[2] != None and isinstance( callback[1], type(dict()) ):
                kwargv = callback[2]
            else:
                raise TaskExecutionError( "callback argument type error") 

            callback[0](*argv, **kwargv)

        elif callback[0] != None:
            raise TaskExecutionError( "callback is not callable") 
    
    @property
    def dataObjects( self ):
        return [ o for o in self._pypeObjects.values( ) if isinstance( o, PypeDataObjectBase )]
    
    @property
    def tasks( self ):
        return [ o for o in self._pypeObjects.values( ) if isinstance( o, PypeTaskBase )]

    @property
    def inputDataObjects(self):
        graph = self._RDFGraph
        inputObjs = []
        for obj in self.dataObjects:
            r = graph.query('SELECT ?o WHERE {<%s> pype:prereq ?o .  }' % obj.URL, initNs=dict(pype=pypeNS))
            if len(r) == 0:
                inputObjs.append(obj)
        return inputObjs
     
    @property
    def outputDataObjects(self):
        graph = self._RDFGraph
        outputObjs = []
        for obj in self.dataObjects:
            r = graph.query('SELECT ?s WHERE {?s pype:prereq <%s> .  }' % obj.URL, initNs=dict(pype=pypeNS))
            if len(r) == 0:
                outputObjs.append(obj)
        return outputObjs

def PypeMPWorkflow(URL = None, **attributes):
    """Factory for the workflow using multiprocessing.
    """
    th = _PypeProcsHandler()
    mq = multiprocessing.Queue()
    se = multiprocessing.Event()
    return _PypeConcurrentWorkflow(URL=URL, thread_handler=th, messageQueue=mq, shutdown_event=se,
            attributes=attributes)

def PypeThreadWorkflow(URL = None, **attributes):
    """Factory for the workflow using threading.
    """
    th = _PypeThreadsHandler()
    mq = Queue.Queue()
    se = threading.Event()
    return _PypeConcurrentWorkflow(URL=URL, thread_handler=th, messageQueue=mq, shutdown_event=se,
            attributes=attributes)

class _PypeConcurrentWorkflow(PypeWorkflow):
    """ 
    Representing a PypeWorkflow that can excute tasks concurrently using threads. It
    assume all tasks block until they finish. PypeTask and PypeDataObjects can be added
    into the workflow and executed through the instance methods.
    """

    CONCURRENT_THREAD_ALLOWED = 16
    MAX_NUMBER_TASK_SLOT = CONCURRENT_THREAD_ALLOWED

    @classmethod
    def setNumThreadAllowed(cls, nT, nS):
        """
        Override the default number of threads used to run the tasks with this method.
        """
        cls.CONCURRENT_THREAD_ALLOWED = nT
        cls.MAX_NUMBER_TASK_SLOT = nS

    def __init__(self, URL, thread_handler, messageQueue, shutdown_event, attributes):
        PypeWorkflow.__init__(self, URL, **attributes )
        self.thread_handler = thread_handler
        self.messageQueue = messageQueue
        self.shutdown_event = shutdown_event
        self.jobStatusMap = dict()

    def addTasks(self, taskObjs):
        """
        Add tasks into the workflow. The dependent input and output data objects are added automatically too. 
        It sets the message queue used for communicating between the task thread and the main thread. One has
        to use addTasks() or addTask() to add task objects to a threaded workflow.
        """
        for taskObj in taskObjs:
            if isinstance(taskObj, PypeTaskCollection):
                for subTaskObj in taskObj.getTasks() + taskObj.getScatterGatherTasks():
                    if not isinstance(subTaskObj, PypeThreadTaskBase):
                        raise TaskTypeError("Only PypeThreadTask can be added into a PypeThreadWorkflow. The task object %s has type %s " % (subTaskObj.URL, repr(type(subTaskObj))))
                    subTaskObj.setMessageQueue(self.messageQueue)
                    subTaskObj.setShutdownEvent(self.shutdown_event)
            else:
                if not isinstance(taskObj, PypeThreadTaskBase):
                    raise TaskTypeError("Only PypeThreadTask can be added into a PypeThreadWorkflow. The task object has type %s " % repr(type(taskObj)))
                taskObj.setMessageQueue(self.messageQueue)
                taskObj.setShutdownEvent(self.shutdown_event)

        PypeWorkflow.addTasks(self, taskObjs)

    def refreshTargets(self, objs=None,
                       callback=(None, None, None),
                       updateFreq=None,
                       exitOnFailure=True):
        if objs is None:
            objs = []
        task2thread = {}
        try:
            rtn = self._refreshTargets(task2thread, objs = objs, callback = callback, updateFreq = updateFreq, exitOnFailure = exitOnFailure)
            return rtn
        except:
            tb = traceback.format_exc()
            self.shutdown_event.set()
            logger.critical("Any exception caught in RefreshTargets() indicates an unrecoverable error. Shutting down...")
            shutdown_msg = """
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
            "! Please wait for all threads / processes to terminate !"
            "! Also, maybe use 'ps' or 'qstat' to check all threads,!"
            "! processes and/or jobs are terminated cleanly.        !"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
            """
            import warnings
            warnings.warn(shutdown_msg)
            th = self.thread_handler
            threads = list(task2thread.values())
            logger.warning("#tasks=%d, #alive=%d" %(len(threads), th.alive(threads)))
            try:
                while th.alive(threads):
                    th.join(threads, 2)
                    logger.warning("Now, #tasks=%d, #alive=%d" %(len(threads), th.alive(threads)))
            except (KeyboardInterrupt, SystemExit) as e:
                logger.debug("Interrupted while joining threads (while handling exception from RefreshTargets()). Trying to terminate any working processes before final exit.")
                th.notifyTerminate(threads)
            raise Exception('Caused by:\n' + tb)


    def _refreshTargets(self, task2thread, objs,
                        callback,
                        updateFreq,
                        exitOnFailure):
        thread = self.thread_handler.create

        rdfGraph = self._RDFGraph # expensive to recompute, should not change during execution
        tSortedURLs = self.getSortedURLs(rdfGraph, objs)

        sortedTaskList = [ (str(u), self._pypeObjects[u], self._pypeObjects[u].getStatus()) for u in tSortedURLs
                            if isinstance(self._pypeObjects[u], PypeTaskBase) ]
        self.jobStatusMap = dict( ( (t[0], t[2]) for t in sortedTaskList ) )
        logger.info("# of tasks in complete graph: %d" %(
            len(sortedTaskList),
            ))

        prereqJobURLMap = {}

        for URL, taskObj, tStatus in sortedTaskList:
            prereqJobURLs = [str(u) for u in rdfGraph.transitive_objects(URIRef(URL), pypeNS["prereq"])
                                    if isinstance(self._pypeObjects[str(u)], PypeTaskBase) and str(u) != URL ]

            prereqJobURLMap[URL] = prereqJobURLs

            logger.debug("Determined prereqs for %r to be %r" % (URL, ", ".join(prereqJobURLs)))

            if taskObj.nSlots > self.MAX_NUMBER_TASK_SLOT:
                raise TaskExecutionError("%s requests more %s task slots which is more than %d task slots allowed" %
                                          (str(URL), taskObj.nSlots, self.MAX_NUMBER_TASK_SLOT) )

        sleep_time = 0
        nSubmittedJob = 0
        usedTaskSlots = 0
        loopN = 0
        lastUpdate = None
        activeDataObjs = set() #keep a set of output data object. repeats are illegal.
        mutableDataObjs = set() #keep a set of mutable data object. a task will be delayed if a running task has the same output.
        updatedTaskURLs = set() #to avoid extra stat-calls
        failedJobCount = 0
        succeededJobCount = 0
        jobsReadyToBeSubmitted = []

        while 1:

            loopN += 1
            if not ((loopN - 1) & loopN):
                # exponential back-off for logging
                logger.info("tick: %d, #updatedTasks: %d, sleep_time=%f" %(loopN, len(updatedTaskURLs), sleep_time))

            for URL, taskObj, tStatus in sortedTaskList:
                if self.jobStatusMap[URL] != TaskInitialized:
                    continue
                logger.debug(" #outputDataObjs: %d; #mutableDataObjs: %d" %(
                    len(taskObj.outputDataObjs.values()),
                    len(taskObj.mutableDataObjs.values()),
                    ))
                prereqJobURLs = prereqJobURLMap[URL]

                logger.debug(' preqs of %s:' %URL)
                for u in prereqJobURLs:
                    logger.debug('  %s: %s' %(self.jobStatusMap[u], u))
                if any(self.jobStatusMap[u] != "done" for u in prereqJobURLs):
                    # Note: If self.jobStatusMap[u] raises, then the sorting was wrong.
                    #logger.debug('Prereqs not done! %s' %URL)
                    continue
                # Check for mutable collisions; delay task if any.
                outputCollision = False
                for dataObj in taskObj.mutableDataObjs.values():
                    for fromTaskObjURL, mutableDataObjURL in mutableDataObjs:
                        if dataObj.URL == mutableDataObjURL and taskObj.URL != fromTaskObjURL:
                            logger.debug("mutable output collision detected for data object %r betw %r and %r" %(
                                dataObj, dataObj.URL, mutableDataObjURL))
                            outputCollision = True
                            break
                if outputCollision:
                    continue
                # Check for illegal collisions.
                if len(activeDataObjs) < 100:
                    # O(n^2) on active tasks, but pretty fast.
                    for dataObj in taskObj.outputDataObjs.values():
                        for fromTaskObjURL, activeDataObjURL in activeDataObjs:
                            if dataObj.URL == activeDataObjURL and taskObj.URL != fromTaskObjURL:
                                raise Exception("output collision detected for data object %r betw %r and %r" %(
                                    dataObj, dataObj.URL, activeDataObjURL))
                # We use 'updatedTaskURLs' to short-circuit 'isSatisfied()', to avoid many stat-calls.
                # Note: Sorting should prevent FileNotExistError in isSatisfied().
                if not (set(prereqJobURLs) & updatedTaskURLs) and taskObj.isSatisfied():
                    #taskObj.setStatus(pypeflow.task.TaskDone) # Safe b/c thread is not running yet, and all prereqs are done.
                    logger.info(' Skipping already done task: %s' %(URL,))
                    logger.debug(' (Status was %s)' %(self.jobStatusMap[URL],))
                    taskObj.setStatus(TaskDone) # to avoid re-stat on subsequent call to refreshTargets()
                    self.jobStatusMap[str(URL)] = TaskDone # to avoid re-stat on *this* call
                    successfullTask = self._pypeObjects[URL]
                    successfullTask.finalize()
                    continue
                self.jobStatusMap[str(URL)] = "ready" # in case not all ready jobs are given threads immediately, to avoid re-stat
                jobsReadyToBeSubmitted.append( (URL, taskObj) )
                for dataObj in taskObj.outputDataObjs.values():
                    logger.debug( "add active data obj: %s" %(dataObj,))
                    activeDataObjs.add( (taskObj.URL, dataObj.URL) )
                for dataObj in taskObj.mutableDataObjs.values():
                    logger.debug( "add mutable data obj: %s" %(dataObj,))
                    mutableDataObjs.add( (taskObj.URL, dataObj.URL) )

            logger.debug( "#jobsReadyToBeSubmitted: %d" % len(jobsReadyToBeSubmitted) )

            numAliveThreads = self.thread_handler.alive(task2thread.values())
            logger.debug( "Total # of running threads: %d; alive tasks: %d; sleep=%f, loopN=%d" % (
                threading.activeCount(), numAliveThreads, sleep_time, loopN) )
            if numAliveThreads == 0 and len(jobsReadyToBeSubmitted) == 0 and self.messageQueue.empty(): 
                #TODO: better job status detection. messageQueue should be empty and all return condition should be "done", or "fail"
                logger.info( "_refreshTargets() finished with no thread running and no new job to submit" )
                for URL in task2thread:
                    assert self.jobStatusMap[str(URL)] in ("done", "fail"), "status(%s)==%r" %(
                            URL, self.jobStatusMap[str(URL)])
                break # End of loop!

            while jobsReadyToBeSubmitted:
                URL, taskObj  = jobsReadyToBeSubmitted[0]
                numberOfEmptySlot = self.MAX_NUMBER_TASK_SLOT - usedTaskSlots 
                logger.debug( "#empty_slots = %d/%d; #jobs_ready=%d" % (numberOfEmptySlot, self.MAX_NUMBER_TASK_SLOT, len(jobsReadyToBeSubmitted)))
                if numberOfEmptySlot >= taskObj.nSlots and numAliveThreads < self.CONCURRENT_THREAD_ALLOWED:
                    t = thread(target = taskObj)
                    t.start()
                    task2thread[URL] = t
                    nSubmittedJob += 1
                    usedTaskSlots += taskObj.nSlots
                    numAliveThreads += 1
                    self.jobStatusMap[URL] = "submitted"
                    # Note that we re-submit completed tasks whenever refreshTargets() is called.
                    logger.debug("Submitted %r" %URL)
                    logger.debug(" Details: %r" %taskObj)
                    jobsReadyToBeSubmitted.pop(0)
                else:
                    break

            time.sleep(sleep_time)
            if updateFreq != None:
                elapsedSeconds = updateFreq if lastUpdate==None else (datetime.datetime.now()-lastUpdate).seconds
                if elapsedSeconds >= updateFreq:
                    self._update( elapsedSeconds )
                    lastUpdate = datetime.datetime.now( )

            sleep_time = sleep_time + 0.1 if (sleep_time < 1) else 1
            while not self.messageQueue.empty():
                sleep_time = 0 # Wait very briefly while messages are coming in.
                URL, message = self.messageQueue.get()
                updatedTaskURLs.add(URL)
                self.jobStatusMap[str(URL)] = message
                logger.debug("message for %s: %r" %(URL, message))

                if message in ["done"]:
                    successfullTask = self._pypeObjects[str(URL)]
                    nSubmittedJob -= 1
                    usedTaskSlots -= successfullTask.nSlots
                    logger.info("Success (%r). Joining %r..." %(message, URL))
                    task2thread[URL].join(timeout=10)
                    #del task2thread[URL]
                    succeededJobCount += 1
                    successfullTask.finalize()
                    for o in successfullTask.outputDataObjs.values():
                        activeDataObjs.remove( (successfullTask.URL, o.URL) )
                    for o in successfullTask.mutableDataObjs.values():
                        mutableDataObjs.remove( (successfullTask.URL, o.URL) )
                elif message in ["fail"]:
                    failedTask = self._pypeObjects[str(URL)]
                    nSubmittedJob -= 1
                    usedTaskSlots -= failedTask.nSlots
                    logger.info("Failure (%r). Joining %r..." %(message, URL))
                    task2thread[URL].join(timeout=10)
                    #del task2thread[URL]
                    failedJobCount += 1
                    failedTask.finalize()
                    for o in failedTask.outputDataObjs.values():
                        activeDataObjs.remove( (failedTask.URL, o.URL) )
                    for o in failedTask.mutableDataObjs.values():
                        mutableDataObjs.remove( (failedTask.URL, o.URL) )
                elif message in ["started, runflag: 1"]:
                    logger.info("Queued %s ..." %repr(URL))
                elif message in ["started, runflag: 0"]:
                    logger.debug("Queued %s (already completed) ..." %repr(URL))
                    raise Exception('It should not be possible to start an already completed task.')
                else:
                    logger.warning("Got unexpected message %r from URL %r." %(message, URL))

            for u,s in sorted(self.jobStatusMap.items()):
                logger.debug("task status: %r, %r, used slots: %d" % (str(u),str(s), self._pypeObjects[str(u)].nSlots))

            if failedJobCount != 0 and (exitOnFailure or succeededJobCount == 0):
                raise TaskFailureError("Counted %d failure(s) with 0 successes so far." %failedJobCount)


        for u,s in sorted(self.jobStatusMap.items()):
            logger.debug("task status: %s, %r" % (u, s))

        self._runCallback(callback)
        if failedJobCount != 0:
            # Slightly different exception when !exitOnFailure.
            raise LateTaskFailureError("Counted a total of %d failure(s) and %d success(es)." %(
                failedJobCount, succeededJobCount))
        return True #TODO: There is no reason to return anything anymore.
    
    def _update(self, elapsed):
        """Can be overridden to provide timed updates during execution"""
        pass

    def _graphvizDot(self, shortName=False):

        graph = self._RDFGraph
        dotStr = StringIO()
        shapeMap = {"file":"box", "state":"box", "task":"component"}
        colorMap = {"file":"yellow", "state":"cyan", "task":"green"}
        dotStr.write( 'digraph "%s" {\n rankdir=LR;' % self.URL)


        for URL in self._pypeObjects.keys():
            URLParseResult = urlparse(URL)
            if URLParseResult.scheme not in shapeMap:
                continue
            else:
                shape = shapeMap[URLParseResult.scheme]
                color = colorMap[URLParseResult.scheme]

                s = URL
                if shortName == True:
                    s = URLParseResult.scheme + "://..." + URLParseResult.path.split("/")[-1] 

                if URLParseResult.scheme == "task":
                    jobStatus = self.jobStatusMap.get(URL, None)
                    if jobStatus != None:
                        if jobStatus == "fail":
                            color = 'red'
                        elif jobStatus == "done":
                            color = 'green'
                    else:
                        color = 'white'
                    
                dotStr.write( '"%s" [shape=%s, fillcolor=%s, style=filled];\n' % (s, shape, color))

        for row in graph.query('SELECT ?s ?o WHERE {?s pype:prereq ?o . }', initNs=dict(pype=pypeNS)):
            s, o = row
            if shortName == True:
                s = urlparse(s).scheme + "://..." + urlparse(s).path.split("/")[-1] 
                o = urlparse(o).scheme + "://..." + urlparse(o).path.split("/")[-1] 
            dotStr.write( '"%s" -> "%s";\n' % (o, s))
        for row in graph.query('SELECT ?s ?o WHERE {?s pype:hasMutable ?o . }', initNs=dict(pype=pypeNS)):
            s, o = row
            if shortName == True:
                    s = urlparse(s).scheme + "://..." + urlparse(s).path.split("/")[-1] 
                    o = urlparse(o).scheme + "://..." + urlparse(o).path.split("/")[-1] 
            dotStr.write( '"%s" -- "%s" [arrowhead=both, style=dashed ];\n' % (s, o))
        dotStr.write ("}")
        return dotStr.getvalue()

# For a class-method:
PypeThreadWorkflow.setNumThreadAllowed = _PypeConcurrentWorkflow.setNumThreadAllowed
PypeMPWorkflow.setNumThreadAllowed = _PypeConcurrentWorkflow.setNumThreadAllowed

class _PypeThreadsHandler(object):
    """Stateless method delegator, for injection.
    """
    def create(self, target):
        thread = threading.Thread(target=target)
        thread.daemon = True  # so it will terminate on exit
        return thread
    def alive(self, threads):
        return sum(thread.is_alive() for thread in threads)
    def join(self, threads, timeout):
        then = datetime.datetime.now()
        for thread in threads:
            assert thread is not threading.current_thread()
            if thread.is_alive():
                to = max(0, timeout - (datetime.datetime.now() - then).seconds)
                thread.join(to)
    def notifyTerminate(self, threads):
        """Assume these are daemon threads.
        We will attempt to join them all quickly, but non-daemon threads may
        eventually block the program from quitting.
        """
        self.join(threads, 1)

class _PypeProcsHandler(object):
    """Stateless method delegator, for injection.
    """
    def create(self, target):
        proc = multiprocessing.Process(target=target)
        return proc
    def alive(self, procs):
        return sum(proc.is_alive() for proc in procs)
    def join(self, procs, timeout):
        then = datetime.datetime.now()
        for proc in procs:
            if proc.is_alive():
                proc.join((datetime.datetime.now() - then).seconds)
    def notifyTerminate(self, procs):
        """This can orphan sub-processes.
        """
        for proc in procs:
            if proc.is_alive():
                proc.terminate()


def defaultOutputTemplate(fn):
    return fn + ".out"

def applyFOFN( task_fun = None, 
               fofonFileName = None, 
               outTemplateFunc = defaultOutputTemplate,
               nproc = 8 ):
               
    tasks = getFOFNMapTasks( FOFNFileName = fofonFileName, 
                             outTemplateFunc = outTemplateFunc, 
                             TaskType=PypeThreadTaskBase,
                             parameters = dict(nSlots = 1))( task_fun )

    wf = PypeThreadWorkflow()
    wf.CONCURRENT_THREAD_ALLOWED = nproc 
    wf.MAX_NUMBER_TASK_SLOT = nproc
    wf.addTasks(tasks)
    wf.refreshTargets(exitOnFailure=False)


if __name__ == "__main__":
    import doctest
    doctest.testmod()
