def total(items):
    # TODO: handle None prices
    return sum(i['price'] for i in items)


def label(name):
    # TODO: trim whitespace
    return name.title()
