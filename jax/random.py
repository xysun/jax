# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""JAX pseudo-random number generators (PRNGs).

The JAX PRNG system is based on "Parallel random numbers: as easy as 1, 2, 3"
(Salmon et al. 2011). For details on the design and its motivation, see:

https://github.com/google/jax/blob/master/design_notes/prng.md
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial

import numpy as onp

from . import lax
from . import numpy as np
from . import tree_util
from .api import custom_transforms, defjvp, jit, vmap
from .numpy.lax_numpy import _constant_like, asarray, stack
from jax.lib import xla_bridge
from jax import core
from jax.interpreters import partial_eval as pe
from jax.scipy.special import logit


def PRNGKey(seed):
  """Create a pseudo-random number generator (PRNG) key given an integer seed.

  Args:
    seed: a 64- or 32-bit integer used as the value of the key.

  Returns:
    A PRNG key, which is modeled as an array of shape (2,) and dtype uint32. The
    key is constructed from a 64-bit seed by effectively bit-casting to a pair
    of uint32 values (or from a 32-bit seed by first padding out with zeros).
  """
  if onp.shape(seed):
    raise TypeError("PRNGKey seed must be a scalar.")
  convert = lambda k: lax.reshape(lax.convert_element_type(k, onp.uint32), [1])
  if isinstance(seed, (int, onp.ndarray)):
    # Special handling of raw integer values, which may have be 64bit even
    # when jax_enable_x64=False and we don't want to drop the top 32 bits
    k1 = convert(onp.bitwise_and(onp.right_shift(seed, 32), 0xFFFFFFFF))
  else:
    k1 = convert(lax.shift_right_logical(seed, 32))
  k2 = convert(lax.bitwise_and(seed, 0xFFFFFFFF))
  return lax.concatenate([k1, k2], 0)

def _is_prng_key(key):
  try:
    return key.shape == (2,) and key.dtype == onp.uint32
  except AttributeError:
    return False


### utilities


def _make_rotate_left(dtype):
  if not onp.issubdtype(dtype, onp.integer):
    raise TypeError("_rotate_left only accepts integer dtypes.")
  nbits = onp.array(onp.iinfo(dtype).bits, dtype)

  def _rotate_left(x, d):
    if lax.dtype(d) != lax.dtype(x):
      d = lax.convert_element_type(d, x.dtype)
    return lax.shift_left(x, d) | lax.shift_right_logical(x, nbits - d)
  return _rotate_left


def _bit_stats(bits):
  """This is a debugging function to compute the statistics of bit fields."""
  return onp.array([list(map(int, onp.binary_repr(x, 64))) for x in bits]).mean(0)


### hash function and split


@jit
def threefry_2x32(keypair, count):
  """Apply the Threefry 2x32 hash.

  Args:
    keypair: a pair of 32bit unsigned integers used for the key.
    count: an array of dtype uint32 used for the counts.

  Returns:
    An array of dtype uint32 with the same shape as `count`.
  """
  # Based on ThreeFry2x32 by phawkins@ in //.../xla/client/lib/prng.cc
  key1, key2 = keypair
  if not lax.dtype(key1) == lax.dtype(key2) == lax.dtype(count) == onp.uint32:
    msg = "threefry_2x32 requires uint32 arguments, got {}"
    raise TypeError(msg.format([lax.dtype(x) for x in [key1, key2, count]]))

  rotate_left = _make_rotate_left(lax.dtype(count))

  def apply_round(v, rot):
    v = v[:]
    v[0] = v[0] + v[1]
    v[1] = rotate_left(v[1], rot)
    v[1] = v[0] ^ v[1]
    return v

  odd_size = count.size % 2
  if odd_size:
    x = list(np.split(np.concatenate([count.ravel(), onp.uint32([0])]), 2))
  else:
    x = list(np.split(count.ravel(), 2))

  rotations = [onp.array([13, 15, 26, 6], dtype=onp.uint32),
               onp.array([17, 29, 16, 24], dtype=onp.uint32)]
  ks = [key1, key2, key1 ^ key2 ^ onp.uint32(0x1BD11BDA)]

  def rotate_list(xs):
    return xs[1:] + [xs[0]]

  x[0] = x[0] + ks[0]
  x[1] = x[1] + ks[1]

  def step(i, state):
    x, ks, rotations = state
    for r in rotations[0]:
      x = apply_round(x, r)
    new_x = [x[0] + ks[0], x[1] + ks[1] + asarray(i + 1, dtype=onp.uint32)]
    return new_x, rotate_list(ks), rotate_list(rotations)
  out = np.concatenate(lax.fori_loop(0, 5, step, (x, rotate_list(ks), rotations))[0])
  assert out.dtype == onp.uint32
  return lax.reshape(out[:-1] if odd_size else out, count.shape)


def split(key, num=2):
  """Splits a PRNG key into `num` new keys by adding a leading axis.

  Args:
    key: a PRNGKey (an array with shape (2,) and dtype uint32).
    num: optional, a positive integer indicating the number of keys to produce
      (default 2).

  Returns:
    An array with shape (num, 2) and dtype uint32 representing `num` new keys.
  """
  return _split(key, num)

@partial(jit, static_argnums=(1,))
def _split(key, num):
  counts = lax.tie_in(key, lax.iota(onp.uint32, num * 2))
  return lax.reshape(threefry_2x32(key, counts), (num, 2))


def fold_in(key, data):
  """Folds in data to a PRNG key to form a new PRNG key.

  Args:
    key: a PRNGKey (an array with shape (2,) and dtype uint32).
    data: data to be folded into the key; will be hashed first if not an int.

  Returns:
    A new PRNGKey that is a deterministic function of the inputs and is
    statistically safe for producing a stream of new pseudo-random values.
  """
  return _fold_in(key, data=data)

@pe.semi_transparent_primitive
def _fold_in(key, data):
  if not isinstance(data, int): # TODO do better
    data = hash(data)
  return __fold_in(key, data)

@jit
def __fold_in(key, data):
  key2 = lax.tie_in(key, PRNGKey(data))
  return threefry_2x32(key, key2)


def _random_bits(key, bit_width, shape):
  """Sample uniform random bits of given width and shape using PRNG key."""
  if not _is_prng_key(key):
    raise TypeError("_random_bits got invalid prng key.")
  if bit_width not in (32, 64):
    raise TypeError("requires 32- or 64-bit field width.")
  max_count = (bit_width // 32) * onp.prod(shape)
  if max_count >= onp.iinfo(onp.uint32).max:
    # TODO(mattjj): just split the key here
    raise TypeError("requesting more random bits than a single call provides.")

  counts = lax.tie_in(key, lax.iota(onp.uint32, max_count))
  bits = threefry_2x32(key, counts)
  if bit_width == 64:
    bits = [lax.convert_element_type(x, onp.uint64) for x in np.split(bits, 2)]
    bits = lax.shift_left(bits[0], onp.uint64(32)) | bits[1]
  return lax.reshape(bits, shape)


### random samplers


def _check_shape(name, shape):
  try:
    shape = tuple(map(int, shape))
  except TypeError:
    msg = "{} requires a concrete tuple of integers as shape argument, got {}."
    raise ValueError(msg.format(name, shape))


def uniform(key, shape=(), dtype=onp.float64, minval=0., maxval=1.):
  """Sample uniform random values in [minval, maxval) with given shape/dtype.

  Args:
    key: a PRNGKey used as the random key.
    shape: a tuple of nonnegative integers representing the shape.
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).
    minval: optional, a minimum (inclusive) value for the range (default 0).
    maxval: optional, a maximum (exclusive) value for the range (default 1).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _uniform(key, minval, maxval, shape=shape, dtype=dtype)

@pe.semi_transparent_primitive
def _uniform(key, minval, maxval, shape, dtype):
  return __uniform(key, shape, dtype, minval, maxval)

@partial(jit, static_argnums=(1, 2))
def __uniform(key, shape, dtype, minval, maxval):
  _check_shape("uniform", shape)
  if not onp.issubdtype(dtype, onp.floating):
    raise TypeError("uniform only accepts floating point dtypes.")

  minval = lax.convert_element_type(minval, dtype)
  maxval = lax.convert_element_type(maxval, dtype)
  finfo = onp.finfo(dtype)
  nbits, nmant = finfo.bits, finfo.nmant

  if nbits not in (32, 64):
    raise TypeError("uniform only accepts 32- or 64-bit dtypes.")

  bits = _random_bits(key, nbits, shape)

  # The strategy here is to randomize only the mantissa bits with an exponent of
  # 1 (after applying the bias), then shift and scale to the desired range. The
  # bit-level transformation we use relies on Numpy and XLA having bit-for-bit
  # equivalent float representations, which might not be true on all platforms.
  float_bits = lax.bitwise_or(
      lax.shift_right_logical(bits, onp.array(nbits - nmant, lax.dtype(bits))),
      onp.array(1., dtype).view(onp.uint32 if nbits == 32 else onp.uint64))
  floats = lax.bitcast_convert_type(float_bits, dtype) - onp.array(1., dtype)
  return lax.max(
      minval,
      lax.reshape(floats * (maxval - minval) + minval, shape))


def randint(key, shape, minval, maxval, dtype=onp.int64):
  """Sample uniform random values in [minval, maxval) with given shape/dtype.

  Args:
    key: a PRNGKey used as the random key.
    shape: a tuple of nonnegative integers representing the shape.
    minval: int or array of ints broadcast-compatible with ``shape``, a minimum
      (inclusive) value for the range.
    maxval: int or array of ints broadcast-compatible with  ``shape``, a maximum
      (exclusive) value for the range.
    dtype: optional, an int dtype for the returned values (default int64 if
      jax_enable_x64 is true, otherwise int32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _randint(key, shape, minval, maxval, dtype)

@partial(jit, static_argnums=(1, 4))
def _randint(key, shape, minval, maxval, dtype):
  _check_shape("randint", shape)
  if not onp.issubdtype(dtype, onp.integer):
    raise TypeError("randint only accepts integer dtypes.")

  minval = lax.convert_element_type(minval, dtype)
  maxval = lax.convert_element_type(maxval, dtype)
  nbits = onp.iinfo(dtype).bits

  if nbits not in (32, 64):
    raise TypeError("randint only accepts 32- or 64-bit dtypes.")

  # if we don't have minval < maxval, just always return minval
  # https://github.com/google/jax/issues/222
  maxval = lax.max(lax.add(minval, onp.array(1, dtype)), maxval)

  # This algorithm is biased whenever (maxval - minval) is not a power of 2.
  # We generate double the number of random bits required by the dtype so as to
  # reduce that bias.
  k1, k2 = split(key)
  rbits = lambda key: _random_bits(key, nbits, shape)
  higher_bits, lower_bits = rbits(k1), rbits(k2)

  unsigned_dtype = onp.uint32 if nbits == 32 else onp.uint64
  span = lax.convert_element_type(maxval - minval, unsigned_dtype)

  # To compute a remainder operation on an integer that might have twice as many
  # bits as we can represent in the native unsigned dtype, we compute a
  # multiplier equal to 2**nbits % span (using that nbits is 32 or 64).
  multiplier = lax.rem(onp.array(2**16, unsigned_dtype), span)
  multiplier = lax.rem(lax.mul(multiplier, multiplier), span)
  if nbits == 64:
    multiplier = lax.rem(lax.mul(multiplier, multiplier), span)

  random_offset = lax.add(lax.mul(lax.rem(higher_bits, span), multiplier),
                          lax.rem(lower_bits, span))
  random_offset = lax.rem(random_offset, span)
  return lax.add(minval, lax.convert_element_type(random_offset, dtype))


def shuffle(key, x, axis=0):
  """Shuffle the elements of an array uniformly at random along an axis.

  Args:
    key: a PRNGKey used as the random key.
    x: the array to be shuffled.
    axis: optional, an int axis along which to shuffle (default 0).

  Returns:
    A shuffled version of x.
  """
  return _shuffle(key, x, axis)

@partial(jit, static_argnums=(2,))
def _shuffle(key, x, axis):
  # On parallel architectures, Fisher-Yates is more expensive than doing
  # multiple sorts. This algorithm is based on one developed and analyzed by
  # tjablin@. We sort according to randomly-generated 32bit keys, but those keys
  # may have collisions. If we repeat the process, using fresh 32bit keys for
  # each sort, then whenever all pairs of elements have been assigned distinct
  # keys at some iteration (or equivalently when the strings formed by
  # concatenating the successive keys for each element are all distinct) then we
  # are guaranteed to have a perfect sample (assuming that either the sort is
  # stable or that any bias is not value-dependent). Since checking uniqueness
  # at runtime may be expensive, we use a heuristic static stop criterion
  # developed by tjablin@. See tensorflow/compiler/tf2xla/random_ops.cc for more
  # info, and for the original implementation of this algorithm. See also
  # Section 2 of http://people.csail.mit.edu/costis/6896sp11/lec5s.pdf for
  # another analysis (where the keys are generated one bit at a time).
  exponent = 3  # see tjablin@'s analysis for explanation of this parameter
  uint32max = onp.iinfo(onp.uint32).max
  num_rounds = int(onp.ceil(exponent * onp.log(x.size) / onp.log(uint32max)))

  for _ in range(num_rounds):
    key, subkey = split(key)
    sort_keys = _random_bits(subkey, 32, x.shape)
    _, x = lax.sort_key_val(sort_keys, x, axis)

  return x


def normal(key, shape=(), dtype=onp.float64):
  """Sample standard normal random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    shape: a tuple of nonnegative integers representing the shape.
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _normal(key, shape, dtype)

@partial(jit, static_argnums=(1, 2))
def _normal(key, shape, dtype):
  _check_shape("normal", shape)
  lo = onp.nextafter(onp.array(-1., dtype), 0., dtype=dtype)
  hi = onp.array(1., dtype)
  u = uniform(key, shape, dtype, lo, hi)
  return onp.array(onp.sqrt(2), dtype) * lax.erf_inv(u)


def bernoulli(key, p=onp.float32(0.5), shape=()):
  """Sample Bernoulli random values with given shape and mean.

  Args:
    key: a PRNGKey used as the random key.
    p: optional, an array-like of floating dtype broadcastable to `shape` for
      the mean of the random variables (default 0.5).
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).

  Returns:
    A random array with the specified shape and boolean dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(lax.dtype(p))
  if not onp.issubdtype(dtype, onp.floating):
    msg = "bernoulli probability `p` must have a floating dtype, got {}."
    raise TypeError(msg.format(dtype))
  p = lax.convert_element_type(p, dtype)
  return _bernoulli(key, p, shape)

@partial(jit, static_argnums=(2,))
def _bernoulli(key, p, shape):
  _check_shape("bernoulli", shape)
  shape = shape or onp.shape(p)
  if onp.shape(p) != shape:
    p = np.broadcast_to(p, shape)
  return lax.lt(uniform(key, shape, lax.dtype(p)), p)


def beta(key, a, b, shape=(), dtype=onp.float64):
  """Sample Bernoulli random values with given shape and mean.

  Args:
    key: a PRNGKey used as the random key.
    a: an array-like broadcastable to `shape` and used as the shape parameter
      alpha of the random variables.
    b: an array-like broadcastable to `shape` and used as the shape parameter
      beta of the random variables.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _beta(key, a, b, shape, dtype)

@partial(jit, static_argnums=(3, 4))
def _beta(key, a, b, shape, dtype):
  _check_shape("beta", shape)
  a = lax.convert_element_type(a, dtype)
  b = lax.convert_element_type(b, dtype)
  shape = shape or lax.broadcast_shapes(np.shape(a), np.shape(b))
  key_a, key_b = split(key)
  gamma_a = gamma(key_a, a, shape, dtype)
  gamma_b = gamma(key_b, b, shape, dtype)
  return gamma_a / (gamma_a + gamma_b)


def cauchy(key, shape=(), dtype=onp.float64):
  """Sample Cauchy random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _cauchy(key, shape, dtype)

@partial(jit, static_argnums=(1, 2))
def _cauchy(key, shape, dtype):
  _check_shape("cauchy", shape)
  u = uniform(key, shape, dtype, minval=onp.finfo(dtype).eps, maxval=1.)
  pi = _constant_like(u, onp.pi)
  return lax.tan(lax.mul(pi, lax.sub(u, _constant_like(u, 0.5))))


def dirichlet(key, alpha, shape=(), dtype=onp.float64):
  """Sample Cauchy random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    alpha: an array-like with `alpha.shape[:-1]` broadcastable to `shape` and
      used as the concentration parameter of the random variables.
    shape: optional, a tuple of nonnegative integers representing the batch
      shape (defaults to `alpha.shape[:-1]`).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _dirichlet(key, alpha, shape, dtype)

@partial(jit, static_argnums=(2, 3))
def _dirichlet(key, alpha, shape, dtype):
  _check_shape("dirichlet", shape)
  alpha = asarray(alpha, dtype)
  shape = shape or alpha.shape[:-1]
  gamma_samples = gamma(key, alpha, shape + alpha.shape[-1:], dtype)
  return gamma_samples / np.sum(gamma_samples, axis=-1, keepdims=True)


def exponential(key, shape=(), dtype=onp.float64):
  """Sample Exponential random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _exponential(key, shape, dtype)

@partial(jit, static_argnums=(1, 2))
def _exponential(key, shape, dtype):
  _check_shape("exponential", shape)
  u = uniform(key, shape, dtype)
  # taking 1 - u to move the domain of log to (0, 1] instead of [0, 1)
  return lax.neg(lax.log1p(lax.neg(u)))


def _gamma_one(key, alpha):
  # Ref: A simple method for generating gamma variables, George Marsaglia and Wai Wan Tsang
  # The algorithm can also be founded in:
  # https://en.wikipedia.org/wiki/Gamma_distribution#Generating_gamma-distributed_random_variables
  zero = _constant_like(alpha, 0)
  one = _constant_like(alpha, 1)
  minus_one = _constant_like(alpha, -1)
  one_over_two = _constant_like(alpha, 0.5)
  one_over_three = _constant_like(alpha, 1. / 3.)
  squeeze_const = _constant_like(alpha, 0.0331)
  dtype = lax.dtype(alpha)

  key, subkey = split(key)
  # for alpha < 1, we boost alpha to alpha + 1 and get a sample according to
  # Gamma(alpha) ~ Gamma(alpha+1) * Uniform()^(1 / alpha)
  boost = lax.select(lax.ge(alpha, one),
                     one,
                     lax.pow(uniform(subkey, (), dtype=dtype), lax.div(one, alpha)))
  alpha = lax.select(lax.ge(alpha, one), alpha, lax.add(alpha, one))

  d = lax.sub(alpha, one_over_three)
  c = lax.div(one_over_three, lax.pow(d, one_over_two))

  def _cond_fn(kXVU):
    _, X, V, U = kXVU
    # TODO: use lax.cond when its batching rule is supported
    # The reason is to avoid evaluating second condition which involves log+log
    # if the first condition is satisfied
    cond = lax.bitwise_and(lax.ge(U, lax.sub(one, lax.mul(squeeze_const, lax.mul(X, X)))),
                           lax.ge(lax.log(U), lax.add(lax.mul(X, one_over_two),
                                                      lax.mul(d, lax.add(lax.sub(one, V),
                                                                         lax.log(V))))))
    return cond

  def _body_fn(kXVU):
    def _next_kxv(kxv):
      key = kxv[0]
      key, subkey = split(key)
      x = normal(subkey, (), dtype=dtype)
      v = lax.add(one, lax.mul(x, c))
      return key, x, v

    key = kXVU[0]
    key, x_key, U_key = split(key, 3)
    _, x, v = lax.while_loop(lambda kxv: lax.le(kxv[2], zero), _next_kxv, (x_key, zero, minus_one))
    X = lax.mul(x, x)
    V = lax.mul(lax.mul(v, v), v)
    U = uniform(U_key, (), dtype=dtype)
    return key, X, V, U

  # initial state is chosen such that _cond_fn will return True
  _, _, V, _ = lax.while_loop(_cond_fn, _body_fn, (key, zero, one, _constant_like(alpha, 2)))
  z = lax.mul(lax.mul(d, V), boost)
  return lax.select(lax.eq(z, zero), onp.finfo(z.dtype).tiny, z)


_bivariate_coef = [[0.16009398, -0.094634816, 0.025146379, -0.0030648348,
                    1, 0.3266811, 0.10406087, 0.0014179033],
                   [0.53487893, 0.12980707, 0.06573594, -0.0015649787,
                    0.16639465, 0.020070098, -0.0035938937, -0.00058392601],
                   [0.040121005, -0.0065914079, -0.002628604, -0.0013441777,
                    0.017050642, -0.0021309345, 0.00085092385, -1.5248239e-07]]


def _gamma_grad_one(z, alpha):
    # Ref 1: Pathwise Derivatives Beyond the Reparameterization Trick, Martin & Fritz
    # Ref 2: Case 4 follows https://github.com/fritzo/notebooks/blob/master/gamma-reparameterized.ipynb

    # TODO: use lax.cond instead of lax.while_loop when its batching rule is available
    # See https://github.com/google/jax/issues/490
    def _case1(zagf):
        z, alpha, _, flag = zagf

        # dz = - dCDF(z; a) / pdf(z; a)
        # pdf = z^(a-1) * e^(-z) / Gamma(a)
        # CDF(z; a) = IncompleteGamma(a, z) / Gamma(a)
        # dCDF(z; a) = (dIncompleteGamma - IncompleteGamma * Digamma(a)) / Gamma(a)
        #            =: unnormalized_dCDF / Gamma(a)
        # IncompleteGamma ~ z^a [ 1/a - z/(a+1) + z^2/2!(a+2) - z^3/3!(a+3) + z^4/4!(a+4) - z^5/5!(a+5) ]
        #                 =: z^a * term1
        # dIncompleteGamma ~ z^a * log(z) * term1 - z^a [1/a^2 - z/(a+1)^2 + z^2/2!(a+2)^2
        #                                                - z^3/3!(a+3)^2 + z^4/4!(a+4)^2 - z^5/5!(a+5)^2 ]
        #                  =: z^a * log(z) * term1 - z^a * term2
        # unnormalized_dCDF = z^a { [log(z) - Digamma(a)] * term1 - term2 }
        zi = 1.0
        update = zi / alpha
        term1 = update
        term2 = update / alpha
        for i in range(1, 6):
            zi = -zi * z / i
            update = zi / (alpha + i)
            term1 = term1 + update
            term2 = term2 + update / (alpha + i)

        unnormalized_cdf_dot = np.power(z, alpha) * ((np.log(z) - lax.digamma(alpha)) * term1 - term2)
        unnormalized_pdf = np.power(z, alpha - 1) * np.exp(-z)
        grad = -unnormalized_cdf_dot / unnormalized_pdf

        return z, alpha, grad, ~flag

    def _cond2(zagf):
        z, alpha, _, flag = zagf
        return (~flag) & (alpha > 8.0) & ((z < 0.9 * alpha) | (z > 1.1 * alpha))

    def _case2(zagf):
        z, alpha, _, flag = zagf

        # Formula 58 of [1]
        sqrt_8a = np.sqrt(8 * alpha)
        z_minus_a = z - alpha
        log_z_div_a = np.log(z / alpha)
        sign = np.where(z < alpha, 1.0, -1.0)
        term1 = 4 * (z + alpha) / (sqrt_8a * z_minus_a * z_minus_a)
        term2 = log_z_div_a * (sqrt_8a / z_minus_a + sign * np.power(z_minus_a - alpha * log_z_div_a, -1.5))
        term3 = z * (1.0 + 1.0 / (12 * alpha) + 1.0 / (288 * alpha * alpha)) / sqrt_8a
        grad = (term1 + term2) * term3

        return z, alpha, grad, ~flag

    def _cond3(zagf):
        z, alpha, _, flag = zagf
        return (~flag) & (alpha > 8.0) & (z >= 0.9 * alpha) & (z <= 1.1 * alpha)

    def _case3(zagf):
        z, alpha, _, flag = zagf

        # Formula 59 of [1]
        z_div_a = np.divide(z, alpha)
        aa = alpha * alpha
        term1 = 1440 * alpha + 6 * z_div_a * (53 - 120 * z) - 65 * z_div_a * z_div_a + 3600 * z + 107
        term2 = 1244160 * alpha * aa
        term3 = 1 + 24 * alpha + 288 * aa
        grad = term1 * term3 / term2

        return z, alpha, grad, ~flag

    def _case4(zagf):
        z, alpha, _, flag = zagf

        # Ref [2]
        u = np.log(z / alpha)
        v = np.log(alpha)
        c = []
        for i in range(8):
            c.append(_bivariate_coef[0][i] + u * (_bivariate_coef[1][i] + u * _bivariate_coef[2][i]))
        p = c[0] + v * (c[1] + v * (c[2] + v * c[3]))
        q = c[4] + v * (c[5] + v * (c[6] + v * c[7]))
        grad = np.exp(p / np.maximum(q, 0.01))

        return z, alpha, grad, ~flag

    _, _, grad, flag = lax.while_loop(lambda zagf: (~zagf[3]) & (zagf[0] < 0.8),
                                      _case1,
                                      (z, alpha, 0.0, False))
    _, _, grad, flag = lax.while_loop(_cond2, _case2, (z, alpha, grad, flag))
    _, _, grad, flag = lax.while_loop(_cond3, _case3, (z, alpha, grad, flag))
    _, _, grad, flag = lax.while_loop(lambda zagf: ~zagf[3], _case4, (z, alpha, grad, flag))
    return grad


def _gamma_grad(sample, a):
    samples = np.reshape(sample, -1)
    alphas = np.reshape(a, -1)
    grads = vmap(_gamma_grad_one)(samples, alphas)
    return grads.reshape(a.shape)


@custom_transforms
def _gamma_impl(key, a):
    alphas = np.reshape(a, -1)
    keys = split(key, onp.size(alphas))
    samples = vmap(_gamma_one)(keys, alphas)
    return np.reshape(samples, np.shape(a))


defjvp(_gamma_impl, None,
       lambda tangent, ans, key, a, **kwargs: tangent * _gamma_grad(ans, a))


def gamma(key, a, shape=(), dtype=onp.float64):
  """Sample Gamma random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    a: an array-like broadcastable to `shape` and used as the shape parameter
      of the random variables.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _gamma(key, a, shape, dtype)

@partial(jit, static_argnums=(2, 3))
def _gamma(key, a, shape, dtype):
  _check_shape("gamma", shape)
  a = lax.convert_element_type(a, dtype)
  shape = shape or onp.shape(a)
  if onp.shape(a) != shape:
    a = np.broadcast_to(a, shape)
  return _gamma_impl(key, a)


def gumbel(key, shape=(), dtype=onp.float64):
  """Sample Gumbel random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _gumbel(key, shape, dtype)

@partial(jit, static_argnums=(1, 2))
def _gumbel(key, shape, dtype):
  _check_shape("gumbel", shape)
  return -np.log(-np.log(
      uniform(key, shape, dtype, minval=onp.finfo(dtype).eps, maxval=1.)))


def laplace(key, shape=(), dtype=onp.float64):
  """Sample Laplace random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _laplace(key, shape, dtype)

@partial(jit, static_argnums=(1, 2))
def _laplace(key, shape, dtype):
  _check_shape("laplace", shape)
  u = uniform(
      key, shape, dtype, minval=-1. + np.finfo(dtype).epsneg, maxval=1.)
  return lax.mul(lax.sign(u), lax.log1p(lax.neg(lax.abs(u))))


def logistic(key, shape=(), dtype=onp.float64):
  """Sample logistic random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _logistic(key, shape, dtype)

@partial(jit, static_argnums=(1, 2))
def _logistic(key, shape, dtype):
  _check_shape("logistic", shape)
  return logit(uniform(key, shape, dtype))


def pareto(key, b, shape=(), dtype=onp.float64):
  """Sample Pareto random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    b: an array-like broadcastable to `shape` and used as the shape parameter
      of the random variables.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _pareto(key, b, shape, dtype)

@partial(jit, static_argnums=(2, 3))
def _pareto(key, b, shape, dtype):
  _check_shape("pareto", shape)
  b = lax.convert_element_type(b, dtype)
  shape = shape or onp.shape(b)
  if onp.shape(b) != shape:
    b = np.broadcast_to(b, shape)
  e = exponential(key, shape, dtype)
  return lax.exp(lax.div(e, b))


def t(key, df, shape=(), dtype=onp.float64):
  """Sample Student's t random values with given shape and float dtype.

  Args:
    key: a PRNGKey used as the random key.
    df: an array-like broadcastable to `shape` and used as the shape parameter
      of the random variables.
    shape: optional, a tuple of nonnegative integers representing the shape
      (default scalar).
    dtype: optional, a float dtype for the returned values (default float64 if
      jax_enable_x64 is true, otherwise float32).

  Returns:
    A random array with the specified shape and dtype.
  """
  dtype = xla_bridge.canonicalize_dtype(dtype)
  return _t(key, df, shape, dtype)

@partial(jit, static_argnums=(2, 3))
def _t(key, df, shape, dtype):
  _check_shape("t", shape)
  df = lax.convert_element_type(df, dtype)
  shape = shape or onp.shape(df)
  key_n, key_g = split(key)
  n = normal(key_n, shape, dtype)
  two = _constant_like(n, 2)
  half_df = lax.div(df, two)
  g = gamma(key_n, half_df, shape, dtype)
  return n * np.sqrt(half_df / g)
