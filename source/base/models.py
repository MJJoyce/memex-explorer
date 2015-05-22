"""Base models."""

import os
import subprocess
import shutil

from django.db import models
from django.utils.text import slugify
from django.core.urlresolvers import reverse
from django.core.validators import RegexValidator
from django.core.urlresolvers import reverse
from django.db.models.signals import post_save

from jinja2 import Template
from jinja2.runtime import Context

from django.conf import settings

from task_manager.file_tasks import unzip
from task_manager.tika_tasks import create_index


def alphanumeric_validator():
    return RegexValidator(r'^[a-zA-Z0-9-_ ]+$',
        'Only numbers, letters, underscores, dashes and spaces are allowed.')


def zipped_file_validator():
    return RegexValidator(r'.*\.(ZIP|zip)$',
        'Only compressed archive (.zip) files are allowed.')


class Project(models.Model):
    """Project model.

    Every service that plugs into Memex Explorer should have a
    foreign key relationship to a Project.

    Model Fields
    ------------

    name : str, 64 characters max
    slug : str, 64 characters max
        The `slug` field is derived from `name` on save, and is restricted
        to URL-safe characters.
    description : textfield

    """

    name = models.CharField(max_length=64, unique=True,
        validators=[alphanumeric_validator()])
    slug = models.SlugField(max_length=64, unique=True)
    description = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        self.slug = slugify(unicode(self.name))
        super(Project, self).save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('base:project',
            kwargs=dict(project_slug=self.slug))

    def kibana_url(self):
        return '/{}/kibana/'.format(self.name)

    def __unicode__(self):
        return self.name

class Service(models.Model):
    """
    Represents information about starting a service in a container.
    """
    name = models.CharField(max_length=64, unique=True,
        validators=[alphanumeric_validator()])
    index_url = models.URLField()

    #Only one of the following can be non-blank. This is not enforced yet.
    image = models.TextField(max_length=256, blank=True, null=True)
    build = models.TextField(max_length=265, blank=True, null=True)
    command = models.TextField(max_length=256)


    def create_container_entry(self):
        container = Container.objects.create(
            service = self,
            high_port = None,
            running = True
        )
        return container

    def __unicode__(self):
        return "{} running {}".format(self.name, self.image or self.build)

class ServiceLink(models.Model):
    from_service = models.ForeignKey(Service, related_name='links')
    to_service  = models.ForeignKey(Service)
    alias = models.TextField(max_length=64, null=True, blank=True)
    external = models.BooleanField(default=False)

class ServicePort(models.Model):
    expose_publicly = models.BooleanField(default=False)
    service = models.ForeignKey(Service, related_name='ports')
    internal_port = models.IntegerField(null=False, blank=False)
    service_name = models.TextField(max_length=64, null=True, blank=True)

    def clean(self, *args, **kwargs):
        if ServicePort.objects.filter(service = self.service).filter(expose_publicly = True).exists():
            raise ValidationError("A service can only expose one port publicly. {} already exposes port {}".filter(
              self.service.name, ServicePort.objects.filter(service = self.service).filter(expose_publicly = True).get().internal_port
              ))

    def __unicode__(self):
        return "{} is running on port {}".format(self.service.name, self.internal_port)

    class Meta:
        unique_together = ('service', 'internal_port')

class VolumeMount(models.Model):
    """
    When creating this service, where to mount it in the container.

    TODO: More thinking required
    """
    service = models.ForeignKey(Service)
    mounted_at = models.TextField(max_length=254)
    """Where within the container is it mounted?"""
    located_at = models.TextField(max_length=254)
    """Where on the host is the directory?"""
    read_only = models.BooleanField(default=False)

class EnvVar(models.Model):
    service = models.ForeignKey(Service, related_name='environment_variables')
    name = models.TextField(max_length=64)
    value = models.TextField(max_length=256, default='')

class Container(models.Model):
    """

    """
    NGINX_CONFIG_TEMPLATE_PATH = os.path.join(settings.BASE_DIR, 'base/deploy_templates/nginx-reverse-proxy.conf.jinja2')
    DOCKER_COMPOSE_TEMPLATE_PATH = os.path.join(settings.BASE_DIR, 'base/deploy_templates/docker-compose.yml.jinja2')
    NGINX_CONFIG_DESTINATION_PATH =  os.path.join(settings.BASE_DIR, 'base/nginx-reverse-proxy.conf')
    NGINX_CONFIG_COPY_PATH = '/etc/nginx/sites-enabled/default'
    DOCKER_COMPOSE_DESTINATION_PATH = os.path.join(settings.BASE_DIR, 'base/docker-compose.yml')

    service = models.ForeignKey(Service)
    "What type of service should the container be running?"
    high_port = models.IntegerField(null=True, blank=True)
    "If the service exposes a port, what high port does it end up exposing it on?"
    public_path_base = models.TextField(null=True, blank=True)
    "If the service is supposed to be served to the outside world and has a base url different than /project.name/service.name, what is it?"
    running = models.BooleanField(default=False)
    "Should the container be running?"

    def slug(self):
        return Container.__slug(self.service)

    def public_urlbase(self):
        if self.public_path_base:
            return self.public_path_base
        return "/{}".format(self.service.name)

    def docker_name(self):
        composefile_dir_name = os.path.basename(os.path.dirname(Container.DOCKER_COMPOSE_DESTINATION_PATH))
        return "{}_{}_1".format(composefile_dir_name, self.slug())

    def context_dict(self):
        #TODO: This can be dramatically sped up by actually thinking about db queries and a judicious prefectch_related
        result = {
            'slug':self.slug(),
            'command': self.service.command or '',
            'volumes' : list(VolumeMount.objects.filter(service = self.service).values('located_at', 'mounted_at')),
            'ports': [port[0] for port in ServicePort.objects.filter(service=self.service).values_list('internal_port')],
            'links': [{'name': Container.__slug(link.to_service), 'alias': link.alias or ''} for link in
                        ServiceLink.objects.filter(from_service = self.service)],
            'environment_variables': list(EnvVar.objects.filter(service=self.service).values('name', 'value')),
        }
        if self.service.image:
            result['image'] = self.service.image
        elif self.service.build:
            result['build'] = self.service.build
        else:
            raise ValueError("container {} has neither an image not a build.".format(self.slug()))
        return result

    @staticmethod
    def __slug(service):
        return "{}{}".format(service.name)

    @staticmethod
    def fill_template(source, destination, context_dict):
        template = Template(open(source, 'r').read(), trim_blocks = True, lstrip_blocks = True)
        result = template.render(context_dict)
        with open(destination, 'w') as f:
            f.write(result)
            f.flush()

    @staticmethod
    def generate_container_context():
        containers = Container.objects.filter(running = True).select_related('service').all()
        return {'containers': [container.context_dict() for container in containers]} #this is going to make about 50 queries when it could make 2 or 5.
    @staticmethod
    def docker_compose_path():
        if os.path.exists(os.path.expanduser('~/miniconda/')):
            DOCKER_COMPOSE_PATH=os.path.expanduser('~/miniconda/bin/docker-compose')
        elif os.path.exists(os.path.expanduser('~/anaconda/')):
            DOCKER_COMPOSE_PATH=os.path.expanduser('~/anaconda/bin/docker-compose')
        elif os.path.exists('/miniconda/'):
            DOCKER_COMPOSE_PATH='/miniconda/bin/docker-compose'
        elif os.path.exists('/anaconda/'):
            DOCKER_COMPOSE_PATH='/anaconda/bin/docker-compose'
        else:
            DOCKER_COMPOSE_PATH='docker-compose'
        return DOCKER_COMPOSE_PATH

    @staticmethod
    def create_containers():
        """
        Create a new docker compose file with an entry for every container that is supposed to be running.
        """
        Container.fill_template(Container.DOCKER_COMPOSE_TEMPLATE_PATH, Container.DOCKER_COMPOSE_DESTINATION_PATH, Container.generate_container_context())
        command = ["sudo",Container.docker_compose_path(),"-f",Container.DOCKER_COMPOSE_DESTINATION_PATH,"up","-d","--no-recreate"]
        print(command)
        out = subprocess.check_output(command)

        service_ids = ServicePort.objects.filter(expose_publicly = True).values_list('service_id', flat=True)
        return out

    @staticmethod
    def get_port_mappings():
        service_ports = dict(ServicePort.objects.filter(expose_publicly = True).values_list('service_id', 'internal_port'))
        port_mappings = set()
        for container in Container.objects.filter(service_id__in = service_ports.keys()).filter(running = True).all():
            docker_port_output = subprocess.check_output(['sudo', 'docker', 'port', container.docker_name()])
            for raw_mapping in docker_port_output.split('\n'):
                print(raw_mapping)
                if '/tcp -> 0.0.0.0:' in raw_mapping:
                    if service_ports[container.service_id] in service_ports.values():
                        internal, external = raw_mapping.split('/tcp -> 0.0.0.0:')
                        container.high_port = int(external)
                        container.save()
                        port_mappings.add((container.public_urlbase(), container.high_port))
        return port_mappings

    @staticmethod
    def generate_nginx_context(port_mappings=[]):
        if port_mappings is None:
            port_mappings = Container.get_port_mappings()
        root_port = os.environ.get('ROOT_PORT', '8000')
        hostname = os.environ.get('HOST_NAME', settings.HOSTNAME)
        ip_addr = os.environ.get('IP_ADDR', settings.IP_ADDR)
        return {
            'static_root': settings.STATIC_ROOT,
            'portmaps': [{'port': mapping[1], 'urlbase': mapping[0]}
                                    for mapping in port_mappings]
                , 'root_port': root_port, 'hostname': hostname, 'ip_addr': ip_addr}

    @staticmethod
    def map_public_ports(port_mappings=None):
        """
        Create a new nginx config with an entry for every container that is supposed to be running and has a public path base.
        Then, restart nginx.
        """
        Container.fill_template(Container.NGINX_CONFIG_TEMPLATE_PATH,
            Container.NGINX_CONFIG_DESTINATION_PATH, Container.generate_nginx_context(port_mappings))
        command = ["sudo","cp",Container.NGINX_CONFIG_DESTINATION_PATH, Container.NGINX_CONFIG_COPY_PATH]
        print(command)
        subprocess.check_output(command)
        return subprocess.check_output(["sudo","service","nginx","restart"])


from task_manager.docker_tasks import start_containers


def start_container_celery(sender, instance, **kwargs):
    start_containers.delay(instance)


if settings.DEPLOYMENT:
    post_save.connect(start_container_celery, sender = Project)


def get_zipped_data_path(instance, filename):
    """
    This method must stay outside of the class definition because django
    cannot serialize unbound methods in Python 2:

    https://docs.djangoproject.com/en/dev/topics/migrations/#migration-serializing
    """
    return os.path.join(settings.MEDIA_ROOT, "indices", instance.slug, "zipped_data", filename)


def delete_folder_contents(folder):
    for file in os.listdir(folder):
        file_path = os.path.join(folder, file)
        try:
            if os.path.isfile(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception, e:
            print(e)


class Index(models.Model):
    """Index model.

    The index model keeps track of indices that are made and what files are
    contained within them.

    Model Fields
    ------------

    name : str, 64 characters max
    description : textfield
    uploaded_data : Django FileField
    data_dolder : textfield
    project : fk to base.Project

    """
    def get_dumped_data_path(instance):
        return os.path.join(
            settings.MEDIA_ROOT,
            "indices",
            instance.slug,
            "data"
        )

    name = models.CharField(max_length=64, unique=True,
        validators=[alphanumeric_validator()])
    slug = models.SlugField(max_length=64, unique=True)
    uploaded_data = models.FileField(upload_to=get_zipped_data_path,
        validators=[zipped_file_validator()])
    data_folder = models.TextField(blank=True)
    project = models.ForeignKey(Project)

    def save(self, *args, **kwargs):
        self.slug = slugify(unicode(self.name))
        if self.uploaded_data:
            zipped_data_path = os.path.dirname(get_zipped_data_path(self, self.uploaded_data.name))
            if os.path.isdir(zipped_data_path):
                delete_folder_contents(zipped_data_path)
            super(Index, self).save(*args, **kwargs)
            self.data_folder = self.get_dumped_data_path()
            if os.path.isdir(self.data_folder):
                delete_folder_contents(self.data_folder)
            unzip(self.uploaded_data.name, self.data_folder)
            if settings.DEPLOYMENT:
                create_index.delay(self)
        super(Index, self).save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('base:project',
            kwargs=dict(project_slug=self.project.slug))

