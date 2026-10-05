import logging
import os
import time

from opentelemetry import metrics, trace
from opentelemetry.metrics import Observation
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from portal.app import create_app

service_name = os.getenv("OTEL_SERVICE_NAME", "demo-stagiaire")
resource = Resource.create(
    {
        "service.name": service_name,
        "service.namespace": "stagiaires",
        "deployment.environment.name": "local",
    }
)

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(),
    export_interval_millis=10000,
)
metrics.set_meter_provider(
    MeterProvider(resource=resource, metric_readers=[metric_reader])
)
meter = metrics.get_meter("demo-app")
application_requests = meter.create_counter(
    "app.requests",
    description="Nombre de requetes HTTP par application et code de statut",
)
application_request_duration = meter.create_histogram(
    "app.request.duration",
    unit="ms",
    description="Duree des requetes HTTP",
)
meter.create_observable_gauge(
    "app.heartbeat.timestamp",
    callbacks=[lambda options: [Observation(time.time())]],
    unit="s",
    description="Horodatage du dernier battement de coeur de l'application",
)

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter())
)
set_logger_provider(logger_provider)
logging.basicConfig(level=logging.INFO)
logging.getLogger().addHandler(
    LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
)
logger = logging.getLogger("demo-app")

app = create_app()
FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)


@app.middleware("http")
async def record_application_metrics(request, call_next):
    started = time.perf_counter()
    route = request.scope.get("route")
    route_name = getattr(route, "path", request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        attributes = {
            "route": route_name,
            "status_code": "500",
        }
        application_requests.add(1, attributes)
        application_request_duration.record(
            (time.perf_counter() - started) * 1000, attributes
        )
        raise

    attributes = {
        "route": route_name,
        "status_code": str(response.status_code),
    }
    application_requests.add(1, attributes)
    application_request_duration.record(
        (time.perf_counter() - started) * 1000, attributes
    )
    return response
