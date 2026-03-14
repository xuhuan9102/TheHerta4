# TODO
# 我们不要手动调用这个类，而是在需要抛出异常时，调用SSMTErrorUtils.raise_fatal_error()


# This used to catch any exception in run time and raise it to blender output console.
class Fatal(Exception):
    pass

