#!/usr/bin/env python3.5

import time, datetime
import copy
from pprint import pprint
import zmq
import redis
import random
import configparser
import argparse
import os
import sys
import json

import util
import geo_helper
import contributor_helper
import users_helper
import trendings_helper

configfile = os.path.join(os.environ['DASH_CONFIG'], 'config.cfg')
cfg = configparser.ConfigParser()
cfg.read(configfile)

ONE_DAY = 60*60*24
ZMQ_URL = cfg.get('RedisGlobal', 'zmq_url')
CHANNEL = cfg.get('RedisLog', 'channel')

serv_log = redis.StrictRedis(
        host=cfg.get('RedisGlobal', 'host'),
        port=cfg.getint('RedisGlobal', 'port'),
        db=cfg.getint('RedisLog', 'db'))
serv_redis_db = redis.StrictRedis(
        host=cfg.get('RedisGlobal', 'host'),
        port=cfg.getint('RedisGlobal', 'port'),
        db=cfg.getint('RedisDB', 'db'))

geo_helper = geo_helper.Geo_helper(serv_redis_db, cfg)
contributor_helper = contributor_helper.Contributor_helper(serv_redis_db, cfg)
users_helper = users_helper.Users_helper(serv_redis_db, cfg)
trendings_helper = trendings_helper.Trendings_helper(serv_redis_db, cfg)


def publish_log(zmq_name, name, content, channel=CHANNEL):
    to_send = { 'name': name, 'log': json.dumps(content), 'zmqName': zmq_name }
    serv_log.publish(channel, json.dumps(to_send))

def getFields(obj, fields):
    jsonWalker = fields.split('.')
    itemToExplore = obj
    lastName = ""
    try:
        for i in jsonWalker:
            itemToExplore = itemToExplore[i]
            lastName = i
        if type(itemToExplore) is list:
            return { 'name': lastName , 'data': itemToExplore }
        else:
            return itemToExplore
    except KeyError as e:
        return ""

def noSpaceLower(text):
    return text.lower().replace(' ', '_')

##############
## HANDLERS ##
##############

def handler_log(zmq_name, jsonevent):
    print('sending', 'log')
    return

def handler_dispatcher(zmq_name, jsonObj):
    if "Event" in jsonObj:
        handler_event(zmq_name, jsonObj)

def handler_keepalive(zmq_name, jsonevent):
    print('sending', 'keepalive')
    to_push = [ jsonevent['uptime'] ]
    publish_log(zmq_name, 'Keepalive', to_push)

def handler_user(zmq_name, jsondata):
    action = jsondata['action']
    json_user = jsondata['User']
    json_org = jsondata['Organisation']
    org = json_org['name']
    if action == 'login': #only consider user login
        timestamp = int(time.time())
        users_helper.add_user_login(timestamp, org)
    else:
        pass

def handler_conversation(zmq_name, jsonevent):
    try: #only consider POST, not THREAD
        jsonpost = jsonevent['Post']
    except KeyError:
        return
    print('sending' ,'Post')
    org = jsonpost['org_name']
    categ = None
    action = 'add'
    eventName = 'no name or id yet...'
    contributor_helper.handleContribution(zmq_name, org,
                    'Discussion',
                    None,
                    action,
                    isLabeled=False)
    # add Discussion
    nowSec = int(time.time())
    trendings_helper.addTrendingDisc(eventName, nowSec)

def handler_object(zmq_name, jsondata):
    print('obj')
    return

def handler_sighting(zmq_name, jsondata):
    print('sending' ,'sighting')
    jsonsight = jsondata['Sighting']
    org = jsonsight['Event']['Orgc']['name']
    categ = jsonsight['Attribute']['category']
    try:
        action = jsondata['action']
    except KeyError:
        action = None
    contributor_helper.handleContribution(zmq_name, org, 'Sighting', categ, action, pntMultiplier=2)
    handler_attribute(zmq_name, jsonsight, hasAlreadyBeenContributed=True)

    try:
        timestamp = jsonsight['date_sighting']
    except KeyError:
        pass

    if jsonsight['type'] == "0": # sightings
        trendings_helper.addSightings(timestamp)
    elif jsonsight['type'] == "1": # false positive
        trendings_helper.addFalsePositive(timestamp)

def handler_event(zmq_name, jsonobj):
    #fields: threat_level_id, id, info
    jsonevent = jsonobj['Event']

    #Add trending
    eventName = jsonevent['info']
    timestamp = jsonevent['timestamp']
    trendings_helper.addTrendingEvent(eventName, timestamp)
    try:
        temp = jsonobj['EventTag']
        tags = []
        for tag in temp:
            tags.append(tag['Tag'])
    except KeyError:
        tags = []
    trendings_helper.addTrendingTags(tags, timestamp)

    #redirect to handler_attribute
    if 'Attribute' in jsonevent:
        attributes = jsonevent['Attribute']
        if type(attributes) is list:
            for attr in attributes:
                jsoncopy = copy.deepcopy(jsonobj)
                jsoncopy['Attribute'] = attr
                handler_attribute(zmq_name, jsoncopy)
        else:
            handler_attribute(zmq_name, attributes)

    try:
        action = jsonobj['action']
    except KeyError:
        action = None
    try:
        eventLabeled = len(jsonobj['EventTag']) > 0
    except KeyError:
        eventLabeled = False
    try:
        org = jsonobj['Orgc']['name']
    except KeyError:
        org = None

    if org is not None:
        contributor_helper.handleContribution(zmq_name, org,
                        'Event',
                        None,
                        action,
                        isLabeled=eventLabeled)

def handler_attribute(zmq_name, jsonobj, hasAlreadyBeenContributed=False):
    # check if jsonattr is an attribute object
    if 'Attribute' in jsonobj:
        jsonattr = jsonobj['Attribute']

    #Add trending
    categName = jsonattr['category']
    try:
        timestamp = jsonattr['timestamp']
    except KeyError:
        timestamp = int(time.time())
    trendings_helper.addTrendingCateg(categName, timestamp)
    try:
        temp = jsonattr['Tag']
        tags = []
        for tag in temp:
            tags.append(tag['Tag'])
    except KeyError:
        tags = []
    trendings_helper.addTrendingTags(tags, timestamp)

    to_push = []
    for field in json.loads(cfg.get('Log', 'fieldname_order')):
        if type(field) is list:
            to_join = []
            for subField in field:
                to_join.append(getFields(jsonobj, subField))
            to_add = cfg.get('Log', 'char_separator').join(to_join)
        else:
            to_add = getFields(jsonobj, field)
        to_push.append(to_add)

    #try to get coord from ip
    if jsonattr['category'] == "Network activity":
        geo_helper.getCoordFromIpAndPublish(jsonattr['value'], jsonattr['category'])

    #try to get coord from ip
    if jsonattr['type'] == "phone-number":
        geo_helper.getCoordFromPhoneAndPublish(jsonattr['value'], jsonattr['category'])

    if not hasAlreadyBeenContributed:
        try:
            eventLabeled = len(jsonattr['Tag']) > 0
        except KeyError:
            eventLabeled = False
        try:
            action = jsonobj['action']
        except KeyError:
            action = None
        contributor_helper.handleContribution(zmq_name, jsonobj['Event']['Orgc']['name'],
                            'Attribute',
                            jsonattr['category'],
                            action,
                            isLabeled=eventLabeled)
    # Push to log
    publish_log(zmq_name, 'Attribute', to_push)


###############
## MAIN LOOP ##
###############

def process_log(zmq_name, event):
    event = event.decode('utf8')
    topic, eventdata = event.split(' ', maxsplit=1)
    jsonevent = json.loads(eventdata)
    print(event)
    try:
        dico_action[topic](zmq_name, jsonevent)
    except KeyError as e:
        print(e)


def main(zmqName):
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(ZMQ_URL)
    socket.setsockopt_string(zmq.SUBSCRIBE, '')

    while True:
        try:
            content = socket.recv()
            content.replace(b'\n', b'') # remove \n...
            zmq_name = zmqName
            process_log(zmq_name, content)
        except KeyboardInterrupt:
            return


dico_action = {
        "misp_json":                handler_dispatcher,
        "misp_json_event":          handler_event,
        "misp_json_self":           handler_keepalive,
        "misp_json_attribute":      handler_attribute,
        "misp_json_object":         handler_object,
        "misp_json_sighting":       handler_sighting,
        "misp_json_organisation":   handler_log,
        "misp_json_user":           handler_user,
        "misp_json_conversation":   handler_conversation,
        "misp_json_object_reference": handler_log,
        }


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='A zmq subscriber. It subscribe to a ZNQ then redispatch it to the misp-dashboard')
    parser.add_argument('-n', '--name', required=False, dest='zmqname', help='The ZMQ feed name', default="MISP Standard ZMQ")
    parser.add_argument('-u', '--url', required=False, dest='zmqurl', help='The URL to connect to', default=ZMQ_URL)
    args = parser.parse_args()

    main(args.zmqname)
