#!/usr/bin/python
#    Copyright 2016 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# Configure the Nagios server with the CGI service for passive checks.
# Configure virtual hosts for monitoring the clusters of global services and nodes
#



import logging
import os
import sys
import yaml
import json
# import shutil
import socket
import dateutil.parser
from argparse import ArgumentParser
from salesforce import OAuth2, Client
from datetime import datetime


LOG = None
DELTA_SECONDS=300

def main():
    parser = ArgumentParser()
    parser.add_argument('-c', '--config-file', default='config.yml')

    parser.add_argument('--description', required=True,
                           help='Description (use "-" to use stdin)' )

    parser.add_argument('--notification_type',  required=True,
                           help='Notification type (PROBLEM|RECOVERY|CUSTOM). Nagios variable - $NOTIFICATIONTYPE$" ')

    parser.add_argument('--state',  required=True,
                           help='Service ot Host (OK|WARNING|CRITICAL|UNCKNOWN). Nagios variable - $SERVICESTATE$" or $HOSTSTATE$ ')

    parser.add_argument('--host_name',           required=True,  help='Host name. Nagios variable - $HOSTNAME$')
    parser.add_argument('--service_description', required=False, help='Service Description. Nagios variable - $SERVICEDESC$')
    parser.add_argument('--long_date_time',      required=True,  help='Date and time. Nagios variable - $LONGDATETIME$' )

    parser.add_argument('--syslog', action='store_true', default=False,
                           help='Log to syslog')

    parser.add_argument('--debug', action='store_true', default=False,
                           help='Enable debug log level')

    parser.add_argument('--log_file', default=sys.stdout,
                           help='Log file. default: stdout. Ignored if logging configured to syslog')



    args = parser.parse_args()

    LOG = logging.getLogger()
    if args.syslog:
        handler = logging.SysLogHandler()
    elif (args.log_file != sys.stdout ):
        handler = logging.FileHandler(args.log_file)
    else:
        handler = logging.StreamHandler(sys.stdout)

    if args.debug:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    formatter = logging.Formatter(
        '{} nagios_to_sfdc %(asctime)s %(process)d %(levelname)s %(name)s '
        '[-] %(message)s'.format(socket.getfqdn()),
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    LOG.setLevel(log_level)
    LOG.addHandler(handler)


# Read from stdin if desctiption defined as '-'
    if  args.description == '-':
        args.description = ''.join(sys.stdin.readlines())

# state are mapped to priority 
    state = {
        'OK':       '060 Informational',
        'UNKNOWN':  '070 Unknown',
        'WARNING':  '080 Warning',
        'CRITICAL': '090 Critical',
        }


    nagios_data = {
        'state':             state[str(args.state).upper()],
        'notification_type': args.notification_type,
        'description':       args.description,
        'host_name':         args.host_name,
        'long_date_time':    args.long_date_time,
    }

    if args.service_description:
        nagios_data['service_description'] = args.service_description
    else:
        nagios_data['service_description'] = ''

    LOG.debug('Nagios data: {} '.format(nagios_data))



    with open(args.config_file) as fp:
        config = yaml.load(fp)

    if 'sfdc_organization_id' in config:
        organizationId = config['sfdc_organization_id']
    else:
        organizationId = None

    sfdc_oauth2 = OAuth2(client_id=config['sfdc_client_id'],
                         client_secret=config['sfdc_client_secret'],
                         username=config['sfdc_username'],
                         password=config['sfdc_password'],
                         auth_url=config['sfdc_auth_url'],
                         organizationId = organizationId )

    environment = config['environment']

# Alert ID shoud be uniq for env
    Alert_ID =  '{}--{}'.format(environment,args.host_name)

    if args.service_description:
        nagios_data['service_description'] = args.service_description
        Alert_ID = '{}--{}'.format(Alert_ID, args.service_description)

    LOG.debug('Alert_Id: {} '.format(Alert_ID))


    sfdc_client = Client(sfdc_oauth2)



    payload = {
        'notification_type': args.notification_type,
        'description':       args.description,
        'long_date_time':    args.long_date_time,
         }

    data = {
        'IsMosAlert__c':     'true',
        'Description':       json.dumps(payload, sort_keys=True, indent=4),
        'Alert_ID__c':       Alert_ID,
        'Subject':           Alert_ID,
        'Environment2__c':   environment,
        'Alert_Priority__c': nagios_data['state'],
        'Alert_Host__c':     nagios_data['host_name'],
        'Alert_Service__c':  nagios_data['service_description'],

#        'sort_marker__c': sort_marker,
        }

    feed_data_body = {
        'Description':       payload,
        'Alert_Id__c':       Alert_ID,
        'Environment2__c':   environment,
        'Alert_Priority__c': nagios_data['state'],
        'Status':   'test',

        }



    try:
      new_case = sfdc_client.create_case(data)
    except Exception as E:
       LOG.debug(E)
       sys.exit(1)


#    print(new_case)
    LOG.debug('New Caset status code: {} '.format(new_case.status_code))
    LOG.debug('New Case data: {} '.format(new_case.text))


#  If Case exist
    if (new_case.status_code  == 400) and (new_case.json()[0]['errorCode'] == 'DUPLICATE_VALUE'):
        LOG.debug('Code: {}, Error message: {} '.format(new_case.status_code, new_case.text))
        # Find Case ID
        ExistingCaseId = new_case.json()[0]['message'].split(" ")[-1]
        LOG.debug('ExistingCaseId: {} '.format(ExistingCaseId))
        # Get Case 
        current_case = sfdc_client.get_case(ExistingCaseId).json()
        LOG.debug("Existing Case: \n {}".format(json.dumps(current_case,sort_keys=True, indent=4)))

        LastModifiedDate=current_case['LastModifiedDate']
        Now=datetime.now().replace(tzinfo=None)
        delta = Now - dateutil.parser.parse(LastModifiedDate).replace(tzinfo=None)

        LOG.debug("Check if Case should be marked as OUTDATED. Case modification date is: {} , Now: {} , Delta(sec): {}, OutdateDelta(sec): {}".format(LastModifiedDate, Now, delta.seconds, DELTA_SECONDS))
        if (delta.seconds > DELTA_SECONDS):
            # Old Case is outdated
            new_data = {
               'Alert_Id__c': '{}_closed_at_{}'.format(current_case['Alert_ID__c'],datetime.strftime(datetime.now(), "%Y.%m.%d-%H:%M:%S")),
               'Alert_Priority__c': '000 OUTDATED',
            }
            u = sfdc_client.update_case(id=ExistingCaseId, data=new_data)
            LOG.debug('Upate status code: {} \n\nUpate content: {}\n\n Upate headers: {}\n\n'.format(u.status_code,u.content, u.headers))

            # Try to create new caset again 
            try:
                new_case = sfdc_client.create_case(data)
            except Exception as E:
                LOG.debug(E)
                sys.exit(1)
            else:
               # Case was outdated an new was created
                CaseId = new_case.json()['id']
                LOG.debug("Case was just created, old one was marked as Outdated")
                # Add commnet, because Case head should conains  LAST data  overriden on any update
                CaseId = new_case.json()['id']

                feeditem_data = {
                  'ParentId':   CaseId,
                  'Visibility': 'AllUsers',
                  'Body': json.dumps(feed_data_body, sort_keys=True, indent=4),
                }
                LOG.debug("FeedItem Data: {}".format(json.dumps(feeditem_data, sort_keys=True, indent=4)))
                add_feed_item = sfdc_client.create_feeditem(feeditem_data)
                LOG.debug('Add FeedItem status code: {} \n Add FeedItem reply: {} '.format(add_feed_item.status_code, add_feed_item.text))

        else:
            # Update Case
            u = sfdc_client.update_case(id=ExistingCaseId, data=data)
            LOG.debug('Upate status code: {} '.format(u.status_code))

            feeditem_data = {
                'ParentId':   ExistingCaseId,
                'Visibility': 'AllUsers',
                'Body': json.dumps(feed_data_body, sort_keys=True, indent=4),
            }

            LOG.debug("FeedItem Data: {}".format(json.dumps(feeditem_data, sort_keys=True, indent=4)))
            add_feed_item = sfdc_client.create_feeditem(feeditem_data)
            LOG.debug('Add FeedItem status code: {} \n Add FeedItem reply: {} '.format(add_feed_item.status_code, add_feed_item.text))

# Else If Case did not exist before and was just  created
    elif  (new_case.status_code  == 201):
        LOG.debug("Case was just created")
        # Add commnet, because Case head should conains  LAST data  overriden on any update
        CaseId = new_case.json()['id']
        feeditem_data = {
          'ParentId':   CaseId,
          'Visibility': 'AllUsers',
          'Body': json.dumps(feed_data_body, sort_keys=True, indent=4),
        }
        LOG.debug("FeedItem Data: {}".format(json.dumps(feeditem_data, sort_keys=True, indent=4)))
        add_feed_item = sfdc_client.create_feeditem(feeditem_data)
        LOG.debug('Add FeedItem status code: {} \n Add FeedItem reply: {} '.format(add_feed_item.status_code, add_feed_item.text))

    else:
        LOG.debug("Unexpected error: Case was not created (code !=201) and Case does not exist (code != 400)")
        sys.exit(1)


if __name__ == '__main__':
    main()

