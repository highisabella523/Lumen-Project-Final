# Production deployment

This deployment uses the root `Dockerfile` and `railway.json`. The container
starts the application with:

```sh
python main.py
```

Do not replace this with `uvicorn main:app`: `main.py` deliberately prepares
the relay's tuned dual-stack listening socket before starting Uvicorn.
Railway supplies `PORT`; it must not be set to `443`. Railway terminates public
HTTPS/WSS on the service domain and forwards traffic to this HTTP listener.

