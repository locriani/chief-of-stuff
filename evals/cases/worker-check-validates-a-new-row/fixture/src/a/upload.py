import os


def save(name, data):
    path = os.path.join('/srv/uploads', name)
    with open(path, 'wb') as f:
        f.write(data)
    return path
