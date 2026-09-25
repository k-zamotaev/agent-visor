"""Process ownership of an AgentVisor data directory, without runtime dependencies."""
import errno
import os
from pathlib import Path


class InstanceInUseError(RuntimeError):
    pass


def lock_instance(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / 'instance.lock').open('a+b')
    try:
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            # Windows can lock beyond EOF; probing must not append to the file.
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        lock.close()
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise InstanceInUseError(
                f'Каталог данных уже используется другим экземпляром AgentVisor: {directory}'
            ) from None
        raise
    return lock
