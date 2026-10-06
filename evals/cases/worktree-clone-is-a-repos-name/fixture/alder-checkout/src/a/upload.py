def upload(request):
    path = request.get('path')
    return open(path, 'wb')
