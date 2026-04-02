import xxhash

def file_hash(path):

    h = xxhash.xxh64()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)

    return h.hexdigest()