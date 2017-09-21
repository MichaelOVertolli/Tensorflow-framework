from .. import config_base as cb
from ..errors import ConfigError


def config(type_):
    config = cb.Config()

    config.g_lr = 8e-5
    config.d_lr = 8e-5
    config.lr_lower_boundary = 2e-5
    config.gamma = 0.7
    config.lambda_k = 0.001
    config.data_format = 'NCHW'

    config.name = type_

    return config