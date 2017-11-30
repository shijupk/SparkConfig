#!/usr/bin/env python3
"""Read data from D-Link motion sensor."""

import xml
import hmac
import urllib
import logging
import asyncio
import functools
import aiohttp
import xml.etree.ElementTree as ET

from io import BytesIO
from datetime import datetime

import xmltodict

_LOGGER = logging.getLogger(__name__)

ACTION_BASE_URL = 'http://purenetworks.com/HNAP1/'


def _hmac(key, message):
    return hmac.new(key.encode('utf-8'),
                    message.encode('utf-8')).hexdigest().upper()


class AuthenticationError(Exception):
    """Thrown when login fails."""

    pass


class HNAPClient:
    """Client for the HNAP protocol."""

    def __init__(self, soap, username, password, loop=None):
        """Initialize a new HNAPClient instance."""
        self.username = username
        self.password = password
        self.logged_in = False
        self.loop = loop or asyncio.get_event_loop()
        self.actions = None
        self._client = soap
        self._private_key = None
        self._cookie = None
        self._auth_token = None
        self._timestamp = None

    @asyncio.coroutine
    def login(self):
        """Authenticate with device and obtain cookie."""
        self.logged_in = False
        resp = yield from self.call(
            'Login', Action='request', Username=self.username,
            LoginPassword='', Captcha='')

        challenge = resp['Challenge']
        public_key = resp['PublicKey']
        self._cookie = resp['Cookie']
        _LOGGER.debug('Challenge: %s, Public key: %s, Cookie: %s',
                      challenge, public_key, self._cookie)

        self._private_key = _hmac(public_key + str(self.password), challenge)
        _LOGGER.debug('Private key: %s', self._private_key)

        try:
            password = _hmac(self._private_key, challenge)
            resp = yield from self.call(
                'Login', Action='login', Username=self.username,
                LoginPassword=password, Captcha='')

            if resp['LoginResult'].lower() != 'success':
                raise AuthenticationError('Incorrect username or password')

            if not self.actions:
                self.actions = yield from self.device_actions()

        except xml.parsers.expat.ExpatError:
            raise AuthenticationError('Bad response from device')

        self.logged_in = True

    @asyncio.coroutine
    def device_actions(self):
        actions = yield from self.call('GetDeviceSettings')
        return list(map(lambda x: x[x.rfind('/')+1:],
                        actions['SOAPActions']['string']))

    @asyncio.coroutine
    def soap_actions(self, module_id):
        return (yield from self.call(
            'GetModuleSOAPActions', ModuleID=module_id))

    @asyncio.coroutine
    def call(self, method, *args, **kwargs):
        """Call an NHAP method (async)."""
        # Do login if no login has been done before
        if not self._private_key and method != 'Login':
            yield from self.login()

        # TODO: Handle exceptions here(?)
        self._update_nauth_token(method)
        return (yield from self.soap().call(method, **kwargs))

    def _update_nauth_token(self, action):
        """Update NHAP auth token for an action."""
        if not self._private_key:
            return

        self._timestamp = int(datetime.now().timestamp())
        self._auth_token = _hmac(
            self._private_key,
            '{0}"{1}{2}"'.format(self._timestamp, ACTION_BASE_URL, action))
        _LOGGER.debug('Generated new token for %s: %s (time: %d)',
                      action, self._auth_token, self._timestamp)

    def soap(self):
        """Get SOAP client with updated headers."""
        if self._cookie:
            self._client.headers['Cookie'] = 'uid={0}'.format(self._cookie)
        if self._auth_token:
            self._client.headers['HNAP_AUTH'] = '{0} {1}'.format(
                self._auth_token, self._timestamp)

        return self._client


class MotionSensor:
    """Wrapper class for a motion sensor."""

    def __init__(self, client, module_id=1):
        """Initialize a new MotionSensor instance."""
        self.client = client
        self.module_id = module_id
        self._soap_actions = None

    @asyncio.coroutine
    def latest_trigger(self):
        """Get latest trigger time from sensor."""
        if not self._soap_actions:
            yield from self._cache_soap_actions()

        detect_time = None
        if 'GetLatestDetection' in self._soap_actions:
            resp = yield from self.client.call(
                'GetLatestDetection', ModuleID=self.module_id)
            detect_time = resp['LatestDetectTime']
        else:
            resp = yield from self.client.call(
                'GetMotionDetectorLogs', ModuleID=self.module_id, MaxCount=1,
                PageOffset=1, StartTime=0, EndTime='All')
            if 'MotionDetectorLogList' not in resp:
                _LOGGER.error('log list: ' + str(resp))
            log_list = resp['MotionDetectorLogList']
            detect_time = log_list['MotionDetectorLog']['TimeStamp']

        return datetime.fromtimestamp(float(detect_time))

    @asyncio.coroutine
    def system_log(self):
        resp = yield from self.client.call(
            'GetSystemLogs', MaxCount=100,
            PageOffset=1, StartTime=0, EndTime='All')
        print(resp)

    @asyncio.coroutine
    def _cache_soap_actions(self):
        resp = yield from self.client.soap_actions(self.module_id)
        self._soap_actions = resp['ModuleSOAPList']['SOAPActions']['Action']


class NanoSOAPClient:

    BASE_NS = {'xmlns:soap': 'http://schemas.xmlsoap.org/soap/envelope/',
               'xmlns:xsd': 'http://www.w3.org/2001/XMLSchema',
               'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance'}
    ACTION_NS = {'xmlns': 'http://purenetworks.com/HNAP1/'}

    def __init__(self, address, action, loop=None, session=None):
        self.address = 'http://{0}/HNAP1'.format(address)
        self.action = action
        self.loop = loop or asyncio.get_event_loop()
        self.session = session or aiohttp.ClientSession(loop=loop)
        self.headers = {}

    def _generate_request_xml(self, method, **kwargs):
        body = ET.Element('soap:Body')
        action = ET.Element(method, self.ACTION_NS)
        body.append(action)

        for param, value in kwargs.items():
            element = ET.Element(param)
            element.text = str(value)
            action.append(element)

        envelope = ET.Element('soap:Envelope', self.BASE_NS)
        envelope.append(body)

        f = BytesIO()
        tree = ET.ElementTree(envelope)
        tree.write(f, encoding='utf-8', xml_declaration=True)

        return f.getvalue().decode('utf-8')

    @asyncio.coroutine
    def call(self, method, **kwargs):
        xml = self._generate_request_xml(method, **kwargs)

        headers = self.headers.copy()
        headers['SOAPAction'] = '"{0}{1}"'.format(self.action, method)

        resp = yield from self.session.post(
            self.address, data=xml, headers=headers, timeout=4)
        text = yield from resp.text()
        parsed = xmltodict.parse(text)
        if 'soap:Envelope' not in parsed:
            _LOGGER.error("parsed: " + str(parsed))

        return parsed['soap:Envelope']['soap:Body'][method + 'Response']

if __name__ == '__main__':
    loop = asyncio.get_event_loop()

    import sys
    address = sys.argv[1]
    pin = sys.argv[2]
    cmd = sys.argv[3]

    @asyncio.coroutine
    def _print_latest_motion():
        session = aiohttp.ClientSession()
        soap = NanoSOAPClient(
            address, ACTION_BASE_URL, loop=loop, session=session)
        client = HNAPClient(soap, 'Admin', pin, loop=loop)
        motion = MotionSensor(client)
        yield from client.login()

        if cmd == 'latest_motion':
            latest = yield from motion.latest_trigger()
            print('Latest time: ' + str(latest))
        elif cmd == 'actions':
            print('Supported actions:')
            print('\n'.join(client.actions))
        elif cmd == 'log':
            log = yield from motion.system_log()

        session.close()

    loop.run_until_complete(_print_latest_motion())
