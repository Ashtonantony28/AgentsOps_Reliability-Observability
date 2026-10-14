from hypothesis import HealthCheck, settings

settings.register_profile("ci", max_examples=5000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
