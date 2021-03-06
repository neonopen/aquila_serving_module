'''
Class to query a remote Aquila model served using TensorFlow Serving

Note that as the code version changes, code may need to be added to
deal with backwards compatibility when pickling/unpickling. See the
Python pickling docs about those issues.

Copyright: 2016 Neon Labs
Author: Mark Desnoyer (desnoyer@neon-lab.com)
Author: Nick Dufour
'''
import aquila_inference_pb2 
import atexit
import concurrent.futures
import datetime
from grpc.beta import implementations
from grpc.beta.interfaces import ChannelConnectivity
import hashlib
import logging
import numpy as np
import pandas
from PIL import Image
import os
import random
import time
import tempfile
import threading
import tornado.locks
import tornado.gen
import utils.obj
import utils.sync
import weakref

_log = logging.getLogger(__name__)

# MEAN_CHANNEL_VALS are the mean pixel value, per channel, of all of our
# training images. This will remain constant: it's a mean over millions of
# images so is unlikely to change significantly. We won't be recomputing it.
MEAN_CHANNEL_VALS = [[[92.366, 85.133, 81.674]]]
MEAN_CHANNEL_VALS = np.array(MEAN_CHANNEL_VALS).round().astype(np.uint8)

# Valid demographic categories
VALID_GENDER = ['M', 'F', None]
VALID_AGE_GROUP = ['18-19', '20-29', '30-39', '40-49', '50+', None]

def _resize_to(img, w=None, h=None):
  '''
  Resizes the image to a desired width and height. If either is undefined,
  it resizes such that the defined argument is satisfied and preserves aspect
  ratio. If both are defined, resizes to satisfy both arguments without
  preserving aspect ratio.

  Args:
    img: A PIL image.
    w: The desired width.
    h: The desired height.
  '''
  ow, oh = img.size
  asp = float(ow) / oh
  if w is None and h is None:
    # do nothing
    return img
  elif w is None:
    # set the width
    w = int(h * asp)
  elif h is None:
    h = int(w / asp)
  return img.resize((w, h), Image.ANTIALIAS)


def _center_crop_to(img, w, h):
  '''
  Center crops image to desired size. If either dimension of the image is
  already smaller than the desired dimensions, the image is not cropped.

  Args:
    img: A PIL image.
    w: The width desired.
    h: The height desired.
  '''
  ow, oh = img.size
  if ow < w or oh < h:
    return img
  upper = (oh - h) / 2
  lower = upper + h
  left = (ow - w) / 2
  right = left + w
  return img.crop((left, upper, right, lower))


def _pad_to_asp(img, asp):
  '''
  Symmetrically pads an image to have the desired aspect ratio.

  Args:
    img: A PIL image.
    asp: The aspect ratio, a float, as w / h
  '''
  ow, oh = img.size
  oasp = float(ow) / oh
  if asp > oasp:
    # the image is too narrow. Pad out width.
    nw = int(oh * asp)
    left = (nw - ow) / 2
    upper = 0
    newsize = (nw, oh)
  elif asp < oasp:
    # the image is too short. Pad out height.
    nh = int(ow / asp)
    left = 0
    upper = (nh - oh) / 2
    newsize = (ow, nh)
  else:
    return img
  nimg = np.zeros((newsize[1], newsize[0], 3)).astype(np.uint8)
  nimg += MEAN_CHANNEL_VALS
  nimg = Image.fromarray(nimg)
  nimg.paste(img, box=(left, upper))
  return nimg


def _aquila_prep(image):
    '''
    Preprocesses an image so that it is appropriate
    for input into Aquila. Aquila was trained on
    images in RGB order, padded to an aspect ratio of
    16:9 and then resized to 299 x 299. We will replicate
    this here. For now, we assume the image provided has
    been obtained from OpenCV (and so is BGR) and will use
    PIL to prep the image.
    '''
    img = Image.fromarray(image[:,:,::-1])
    img = _pad_to_asp(img, 16./9)
    # resize the image to 299 x 299
    img = _resize_to(img, w=299, h=299)
    return np.array(img).astype(np.uint8)

class DemographicSignatures(object):
    '''Object that manages all the signatures for different demographics.

    dot this vector with your image signature and you get the model
    score for that image for that demographic.
    
    '''
    __metaclass__ = utils.obj.KeyedSingleton

    def __init__(self, model_name):
        # Load up the files
        weights_fn = os.path.join(os.path.dirname(__file__),
                                  '..',
                                  'demographics',
                                  '%s-weight.pkl' % model_name)
        bias_fn = os.path.join(os.path.dirname(__file__),
                               '..',
                               'demographics',
                               '%s-bias.pkl' % model_name)
        try:
            self.weights = pandas.read_pickle(weights_fn)
        except IOError as e:
            _log.error('Could not read a valid model weights file at %s: %s' % 
                       (weights_fn, e))
            raise KeyError(model_name)
        try:
            self.bias = pandas.read_pickle(bias_fn)
        except IOError as e:
            _log.error('Could not read a valid model bias file at %s: %s' % 
                       (bias_fn, e))
            raise KeyError(model_name)

    def _safe_get_weights(self, gender, age):
        if gender is None:
            gender = 'None'
        if age is None:
            age = 'None'
        try:
            return self.weights[gender, age]
        except KeyError as e:
            _log.error_n('Unknown Demographic for weights file: %s,%s' % (gender, age))
            raise

    def _safe_get_bias(self, gender, age):
        if gender is None:
            gender = 'None'
        if age is None:
            age = 'None'
        try:
            return self.bias[gender, age]
        except KeyError as e:
            _log.error_n('Unknown Demographic for bias file: %s,%s' % (gender, age))
            raise

    def compute_score_for_demo(self, X, gender=None, age=None):
        '''Returns the score for gender `gender` and age `age` derived from
        feature vector X (a numpy array)
        '''
        X = pandas.Series(X)

        b = self._safe_get_bias(gender, age)
        W = self._safe_get_weights(gender, age)
        try:
            score = X.dot(W) + b
        except ValueError as e:
            _log.error('Improper feature vector size: %s' % e.message)
            raise ValueError(e)
        # return score
        # for now, we're nto going to return the score as multiindex 
        # series objects, but simply as floats.
        return float(score)

    def get_scores_for_all_demos(self, X):
        '''Returns the scores for all demographics.

        Inputs: X - feature vector reprsenting an image
        
        Returns: A pandas Series with a multiindex for all the demographics
        '''

        X = pandas.Series(X)
        try:
            scores = X.dot(self.weights) + self.bias
        except ValueError as e:
            _log.error('Improper feature vector size: %s' % e.message)
            raise ValueError(e)
        return scores

    def compute_feature_importance(self, X, gender=None, age=None):
        '''Returns the importance of each feature for a given image.

        Inputs:
        X - feature vector representing an image

        Returns: 
        An importance score for each feature as a pandas Series with the 
        index being the index into X. Sorted by importance descending.
        '''
        X = pandas.Series(X)
        W = self._safe_get_weights(gender, age)

        importance = W * X
        return importance.sort(ascending=False, inplace=False)
    
class Predictor(object):
    '''An abstract valence predictor.

    This class should be specialized for specific models
    '''
    def __init__(self, feature_generator = None):
        self.feature_generator = feature_generator
        self.__version__ = 3

        self._executor = concurrent.futures.ThreadPoolExecutor(10)

    def add_feature_vector(self, features, score, metadata=None):
        '''Adds a veature vector to train on.

        Inputs:
        features - a 1D numpy vector of the feature vector
        score - score of this example.
        metadata - metadata to attach to this example
        '''
        raise NotImplementedError()

    def add_image(self, image, score, metadata=None):
        '''Add an image to train on.

        Inputs:
        image - numpy array of the image in BGR format (aka OpenCV)
        score - floating point valence score
        metadata - metadata to attach to this example
        '''
        self.add_feature_vector(self.feature_generator.generate(image),
                                score,
                                metadata=metadata)

    def add_images(self, data):
        '''Adds multiple images to the model.

        Input:
        data - iteration of (image, score) tuples
        '''
        for image, score in data:
            self.add_image(image, score)

    def train(self):
        '''Train on any images that were previously added to the predictor.'''
        raise NotImplementedError()


    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def predict(self, image, ntries=3, timeout=10.0, base_time=0.4, 
                *args, **kwargs):
        '''Predicts the valence score of an image synchronously.

        Inputs:
        image - numpy array of the image

        Returns: (predicted valence score, feature vector, model_version) 
                 any can be None

        Raises: NotTrainedError if it has been called before train() has.
        '''
        cur_try = 0
        kwargs['timeout'] = timeout
        while cur_try < ntries:
            cur_try += 1
            try:
                score, vec, vers = yield self._predict(image,
                                                       *args, **kwargs)
                raise tornado.gen.Return((score, vec, vers))
            except tornado.gen.Return:
                raise
            except PredictionError as e:
                _log.warn('Problem scoring image. Retrying: %s' % e)
                delay = (1 << cur_try) * base_time * random.random()
                yield tornado.gen.sleep(delay)
            except Exception as e:
                _log.exception('Unexpected problem scoring image. Retrying: %s'
                               % e)
                delay = (1 << cur_try) * base_time * random.random()
                yield tornado.gen.sleep(delay)
        if isinstance(e, PredictionError):
            raise e
        raise PredictionError(str(e))

    @tornado.gen.coroutine
    def _predict(self, image, *args, **kwargs):
        '''Predicts the valence score of an image synchronously.

        Inputs:
        image - numpy array of the image

        Returns: (predicted valence score, feature vector, model_version) 
                 any can be None

        Raises: NotTrainedError if it has been called before train() has.
        '''
        raise NotImplementedError()

    def reset(self):
        '''Resets the predictor by removing all the data/model.'''
        raise NotImplementedError()

    def hash_type(self, hashobj):
        '''Updates a hash object with data about the type.'''
        hashobj.update(self.__class__.__name__)
        self.feature_generator.hash_type(hashobj)

    def complete(self):
        '''
        Returns True when all requests are complete.
        '''
        if not self.async:
            # you are running synchronously, so it's fine.
            return True
        else:
            raise NotImplementedError()

    def shutdown(self):
        '''
        There is currently a bug in the gRPC garbage collection,
        so this function is useful to disable stubs and channels
        that are not created within the frame of a function (i.e.,
        if the stub / channel is created in __main__, or as the
        attribute of a class, as in DeepnetPredictor).
        '''
        pass

def deepnet_conn_callback(predictor, status):
    '''A callback that uses a weak reference to avoid a circular reference.'''
    self = predictor()
    if self:
        self._check_conn(status)

class GRPCFutureWrapper(concurrent.futures.Future):
    '''Wraps a GRPCFuture so that it looks like a concurrent one.'''
    def __init__(self, future):
        self._future = future

    def __getattribute__(self, name):
        if name == '_future':
            return super(GRPCFutureWrapper, self).__getattribute__(name)
        return getattr(self._future, name)

class DeepnetPredictor(Predictor):
    '''Prediction using the deepnet Aquila (or an arbitrary predictor).
    Note, this does not require you provision a feature generator for
    the predictor.

    The connection to the server is maintained internally, using the
    _check_conn method. This is called at the outset and invokes
    _connect, which creates the channel and the stub, and adds
    _check_conn as a callback. _check_conn will ensure that the
    state of the ready event is set appropriately as the state of
    the gRPC channel changes.'''

    def __init__(self, concurrency=10, port=9000,
                 aquila_connection=None,
                 gender=None, age=None):
        '''
        concurrency - The maximum number of simultaneous requests to
        submit.
        port - the port on which to establish the connection.
        aquila_connection - An instance (or singleton) of an object
        that supplies the get_ip method, which returns an IP address
        of an Aquila server as a string.
        '''
        super(DeepnetPredictor, self).__init__()
        self.concurrency = concurrency
        self.aq_conn = aquila_connection
        self.port = port
        self._cv = threading.Condition()
        self.active = 0
        self._ready_lock = threading.RLock()
        self._ready = tornado.locks.Event()
        self._shutting_down = False
        # register your own shutdown function to the atexit
        # cleanup handlers, since gRPC currently has issues
        # with stubs & channels that are *attributes* of a
        # class.
        # atexit.register(self.shutdown)
        self.channel = None
        self.stub = None
        self._conn_callback = None
        self._conn_lock = threading.RLock()
        self._consequtive_connection_failures = 0

        # Optional demographic parameters used to get the target
        # vector needed when calculating the model score.
        self.gender = None
        self.age = None

    def _reconnect(self, force_refresh):
        '''
        Establishes a new connection to the server.
        '''
        self._disconnect()
        self.connect(force_refresh)

    def connect(self, force_refresh=False):
        '''Establish a connection to the server if there isn't one.'''
        with self._conn_lock:
            if self.channel is None and not self._shutting_down:
                host = self.aq_conn.get_ip(force_refresh=force_refresh)
                _log.debug('Establishing connection on %s' % host)
                # open question: what happens to futures that derive
                #   from destroyed channels?
                self.channel = implementations.insecure_channel(host,
                                                                self.port)
                # register callback
                weak_self = weakref.ref(self)
                self._conn_callback = lambda status: deepnet_conn_callback(
                    weak_self, status)
                self.channel.subscribe(self._conn_callback,
                                       try_to_connect=True)
                self.stub = aquila_inference_pb2.beta_create_AquilaService_stub(
                    self.channel, pool_size=self.concurrency)

    def _disconnect(self):
        ''' Disconnect from the server if there is a connection. '''
        # the connection has been lost
        with self._conn_lock:
            if self.channel is not None:
                with self._ready_lock:
                    self._ready.clear()
                del self.stub
                self.stub = None
                self.channel.unsubscribe(self._conn_callback)
                self._conn_callback = None
                del self.channel
                self.channel = None

    def _check_conn(self, status):
        '''
        Callback for checking the connection, subsumes the dual callbacks
        we had before.
        '''
        if (status is ChannelConnectivity.TRANSIENT_FAILURE or
            status is ChannelConnectivity.FATAL_FAILURE):
            _log.warn('Lost connection to server, trying another')
            self._consequtive_connection_failures += 1
            time.sleep((self._consequtive_connection_failures << 1) * 0.1 *
                       random.random())
            self._reconnect(force_refresh=True)
        elif self._ready.is_set():
            pass
        elif status is ChannelConnectivity.READY:
            _log.debug('Server has been reached')
            with self._ready_lock:
                self._ready.set()
            self._consequtive_connection_failures = 0
            _log.debug('Ready event is set.')

    @tornado.gen.coroutine
    def _predict(self, image, timeout=10.0):
        '''
        image: The image to be scored, as a OpenCV-style numpy array.
        timeout: How long the request lasts for before expiring.
        '''
        if self._shutting_down:
            raise PredictionError('Object is shutting down.')

        # Wait for the connection to be ready
        with self._ready_lock:
            ready_future = self._ready.wait(datetime.timedelta(seconds=timeout))
        yield ready_future
        
        image = _aquila_prep(image)
        request = aquila_inference_pb2.AquilaRequest()
        request.image_data = image.flatten().tostring()
        # # it appears to be the case that creating the stub as an
        # # attribute can cause some issues, so let's see if this
        # # works.
        # with aquila_inference_pb2.beta_create_AquilaService_stub(self.channel) as stub:
        #     result_future = stub.Regress.future(request, timeout)  # 10 second timeout
        with self._cv:
            self.active += 1
        try:
            response = yield GRPCFutureWrapper(self.stub.Regress.future(
                request, timeout))
        # TODO(mdesnoyer, nick): On upgrade, only catch
        # RpcErrors. Version 0.13 of grpc doesn't have them
        except Exception as e:
            msg = 'RPC Error: %s' % e
            _log.error(msg)
            raise PredictionError(msg)
        finally:
            with self._cv:
                self.active -= 1
                self._cv.notify_all()

        if response is None:
            msg = 'RPC Error: response was None'
            _log.error(msg)
            raise PredictionError(msg)

        vers = response.model_version or 'aqv1.1.250'

        if len(response.valence) == 1:
            # The response is only returning the valence, not the
            # feature vector
            raise tornado.gen.Return((response.valence[0], None, vers))

        features = np.array(response.valence)
        score = None
        if response.model_version is not None:
            try:
                signatures = DemographicSignatures(response.model_version)
                score = signatures.compute_score_for_demo(
                    features, gender=self.gender, age=self.age)
            except KeyError as e:
                # There was some problem obtaining the score.
                _log.warn_n('Unknown model/demographic. model: %s age: %s gender %s'
                            % (response.model_version, self.gender, self.age))
        raise tornado.gen.Return((score, features, vers))

    def complete(self):
        '''
        Blocks until all the currently active jobs are done
        '''
        with self._cv:
            while self.active > 0:
                self._cv.wait()

        return True

    def __del__(self):
        self.shutdown()

    def shutdown(self):
        _log.debug('Exit has started.')
        self._shutting_down = True
        self._disconnect()

# -------------- Start Exception Definitions --------------#

class Error(Exception):
    '''Base class for exceptions in this module.'''
    pass

class NotTrainedError(Error):
    def __init__(self, message = ''):
        Error.__init__(self, "The model isn't trained yet: %s" % message)

class AlreadyTrainedError(Error):
    def __init__(self, message = ''):
        Error.__init__(self, "The model is already trained: %s" % message)

class PredictionError(Error):
    '''An error calculating the prediction.'''
