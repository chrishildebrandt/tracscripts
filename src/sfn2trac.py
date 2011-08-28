#!/usr/bin/env python
"""
Import SourceForge "new" backup file into a Trac database.
Attachments are downloaded from SF server and also imported.
You need to edit settings section below before use.

Author: Marcin Wojdyr <wojdyr@gmail.com>

Based on http://svn.python.org/projects/tracker/importer/ 
and on other Trac import script (bugzilla2trac.py, sourceforge2trac.py, 
mantis2trac.py).

Tested only with Python 2.5, Trac 0.11 + AccountManagerPlugin 

Edit the settings below and run the program without any options.
This will import everything but users. Usernames (not emails) are written 
in ticket and ticket_change tables, but accounts are not created.

It also creates a wiki page in text file (not in the datebase) that maps old 
SF ticket numbers to Trac ticket numbers.

Then make sure that you have AccountManagerPlugin configured with option
password_store=SessionStore
and run this script with -u. This will import users and emails into 
session_attribute table. Email notifications should work at this point.
Users can use "Forgot your password?" option to have a new password sent
to @users.sf.net alias.

Finally, you can generate random passwords and send them to all users - see 
function reset_all_passwords() 

Known issues: 
 * some non-ASCII characters in the XML backup file are encoded incorrectly
 * after finishing import you need to set a default priority in admin panel

$Id$
"""


##################### settings -- edit before running #######################

# Path to the SourceForge's "new" backup file. 
# This file can be downloaded *only by project administrator* from 
# https://sourceforge.net/export/xml_export2.php?group_id=<project group id>
SF_EXPORT2 = "export2.xml"

# Path to the Trac environment.
TRAC_ENV = "/path/to/trac/env/"

IMPORT_SF_TRACKERS = [
  "Bugs",
  "Patches",
  "Feature Requests",
  #"Support Requests"
]

# If true, all existing Trac tickets and attachments will be removed
# prior to import.
TRAC_CLEAN = True

# Enclose imported ticket description and comments in a {{{ }}}
# preformat block?  This formats the text in a fixed-point font.
PREFORMAT_COMMENTS = False

PRIORITIES = [ "blocker", "high", "normal", "low" ]

PRIORITY_MAP = { 1: 'low',
                 2: 'low',
                 3: 'low',
                 4: 'low',
                 5: 'normal',
                 6: 'high',
                 7: 'high',
                 8: 'high',
                 9: 'blocker'
               }

TYPES = ["build error", "defect", "optimization", "enhancement"]

TRACKER_TO_TYPE = { "Bugs": 'defect',
                    "Patches": '', 
                    "Feature Requests": 'enhancement',
                    "Support Requests": ''
                  }

# we could use "patch" keyword to mark patches (like Python's Roundup tracker)
TRACKER_TO_KEYWORD = { # "Patches": "patch"
                     }
# ... but we prefer to have a custom checkbox
# in trac.ini:
#  [ticket-custom]
#  patch = checkbox
#  patch.label = Patch
#  patch.value = 0
USE_PATCH_CHECKBOX = True


COMPONENTS = [
 "", # unknown
 "wxMSW",
 "wxGTK",
 "wxMac",
 "wxMotif"
]

CATEGORY_TO_COMPONENT = {
  "wxMSW specific": "wxMSW",
  "wxGTK specific": "wxGTK",
  "wxMac specific": "wxMac",
  "wxMotif specific": "wxMotif",
  "": ""
}

CATEGORY_TO_KEYWORD = {
  "Printing": "printing",
  "wxImage": "wxImage"
}

RESOLUTIONS = [ "fixed", 
                "invalid", 
                "wontfix", 
                "duplicate", 
                "worksforme", 
                "outdated" ]

RESOLUTIONS_MAP = {
  "fixed" : "fixed",
  "invalid": "invalid",
  "wont fix": "wontfix",
  "later": "",
  "remind": "",
  "works for me": "worksforme",
  "duplicate": "duplicate",
  "accepted": "fixed",
  "out of date": "outdated",
  "postponed": "",
  "rejected": "wontfix",
  "none": ""
}

STATUS_MAP = {
    # there is hack for open status here: special value "new-or-assigned" means
    # that open ticket becomes "assigned" if it has owner, otherwise "new".
    "Open": "new-or-assigned",
    "Closed": "closed",
    "Deleted": "closed",
    "Pending": "closed"
}


# If False, attachments are not imported
HANDLE_ATTACHMENTS = True

# Directory for storing cached files (attachments)
CACHE_DIR = "cache-files"

# Output project members to this file. All users have e-mails 
# username@users.sourceforge.net, so the e-mail is not written
USERS_FILE = "users-list.txt"

# We prefer to have sequential numbering of tickets, so ticket numbers from
# SF are not preserved. A wiki site can be created with a table 
# of corresponding old and new ticket numbers.
NUMBERS_FILE = "Sf.wiki"


# If True, all users listed in a tracker_item (reporter, owner and those 
# who submitted comment or made changes to the ticket) will be added to cc.
CC_EVERYBODY = True

#############################  import script  ###############################

import re
import os
import sys
import time
import urllib
import socket

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import cElementTree as ET

from trac.env import Environment
from trac.attachment import Attachment

comment_junk = re.compile("^Logged In: (YES |NO )\nuser_id=[0-9]+\n"
                           "(Originator: (YES|NO)\n)?", 
                           re.MULTILINE)

def remove_recursively(d):
    " rm -r d"
    for root, dirs, files in os.walk(d, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))


class TracDatabase(object):
    def __init__(self, path):
        self.env = Environment(path)
        self._db = self.env.get_db_cnx()

    def db(self):
        return self._db

    def hasTickets(self):
        c = self.db().cursor()
        c.execute("SELECT count(*) FROM Ticket")
        return int(c.fetchall()[0][0]) > 0

    def setList(self, name, values):
        c = self.db().cursor()
        c.execute("DELETE FROM %s" % name)
        for v in values:
            print "  inserting %s '%s'" % (name, v)
            c.execute("INSERT INTO " + name + " (name) VALUES (%s)", (v,))
        self.db().commit()

    def setEnumList(self, name, values):
        c = self.db().cursor()
        c.execute("DELETE FROM enum WHERE type=%s", (name,))
        for n, v in enumerate(values):
            print "  inserting %s '%s'" % (name, v)
            c.execute("INSERT INTO enum (type, name, value) VALUES (%s,%s,%s)",
                      (name, v, n))
        self.db().commit()

    def clean(self):
        print "\nCleaning all tickets..."
        c = self.db().cursor()
        c.execute("DELETE FROM ticket_change")
        c.execute("DELETE FROM ticket")
        c.execute("DELETE FROM attachment")
        c.execute("DELETE FROM ticket_custom")
        self.db().commit()

        attachments_dir = os.path.join(os.path.normpath(self.env.path),
                                       "attachments")
        remove_recursively(attachments_dir)
        if not os.path.isdir(attachments_dir):
            os.mkdir(attachments_dir)

    def addAttachment(self, ticket_id, filename, datafile, filesize,
                      author, description, upload_time):
        # copied from bugzilla2trac
        attachment = Attachment(self.env, 'ticket', ticket_id)
        attachment.author = author
        attachment.description = description
        attachment.insert(filename, datafile, filesize, upload_time)
        del attachment



def initialize(db):
    if db.hasTickets():
        raise Exception("Will not modify database with existing tickets!")
    db.setEnumList('ticket_type', TYPES)
    db.setList('component', COMPONENTS)
    db.setEnumList('priority', PRIORITIES)
    db.setEnumList('resolution', RESOLUTIONS)


class SfTrackerInfo:
    def __init__(self, tracker):
        self.name = tracker.findtext("name")

        # we can guess what type of issues are in this tracker
        self.type = TRACKER_TO_TYPE[self.name]

        res_all = tracker.find("resolutions").findall("resolution")
        self.resolutions = dict((i.findtext('id'), i.findtext("name")) 
                                for i in res_all)

        st_all = tracker.find("statuses").findall("status")
        self.statuses = dict((i.findtext("id"), i.findtext("name")) 
                             for i in st_all)

        cat_all = tracker.find("categories").findall("category")
        self.categories = dict((i.findtext('id'), i.findtext('category_name')) 
                               for i in cat_all)
        # whatever it means...
        self.categories["100100"] = ""
        self.categories["300100"] = ""
        self.categories["100"] = ""

#copied from xmlexport2handlers.py
def downloadfile(url, cachefilename):
    delay = 0
    backoff = 30
    while True:
        print url, "->", cachefilename
        try:
            f = urllib.urlopen(url)
            data = f.read()
            if data.find("send-email-to-ipblocked-at-sourceforge-dot-net") >= 0:
                delay+=backoff
                print "Blocked by Sourceforge. Sleeping %d seconds "\
                        "before trying again" % delay

            out = open(cachefilename + ".tmp", 'w')
            #out.write(str(f.headers))
            #out.write("\n")
            out.write(data)
            out.close()
            try:
                os.remove(cachefilename)
            except:
                pass
            os.rename(cachefilename + ".tmp", cachefilename)
            break

        except socket.error, e:
            print "Error fetching file, retrying", e
            continue
        except AttributeError, e:
            print e, "Probably SF weirdness. Trying again after delay.."
            delay+=backoff
        except IOError, e:
            print e, "Probably SF weirdness. Trying again after delay.."
            delay+=backoff                

        time.sleep(delay)


def write_users_file(tree):
    print "Writing a list of users to '%s'..." % USERS_FILE
    users_file = file(USERS_FILE, "w")
    ps = tree.find('projectsummary').find("projectmembers")
    members = set()
    # project members are in xml file
    for pm in ps.findall("projectmember"):
        username = pm.findtext('user_name')
        users_file.write("%s:%s:%s\n" % (username,
                               pm.findtext('public_name').encode('utf-8'),
                               pm.findtext('project_admin')))
        members.add(username)
    for tracker in tree.find("trackers").findall("tracker"):
        for item in tracker.find('tracker_items').findall('tracker_item'):
            for i in get_all_involved_users(item):
                if i not in members:
                    members.add(i)
                    users_file.write("%s::No\n" % i)
    users_file.close()


def import_users(db):
    """Read users from file and import into AccountManager. 
       It deletes existing users first. Use with password_store=SessionStore.
    """
    c = db.db().cursor()
    c.execute("DELETE FROM session_attribute WHERE authenticated=1") 
    c.execute("DELETE FROM session WHERE authenticated=1") 

    for i in file(USERS_FILE):
        user, name, is_admin = i.split(':')
        hashed_password = 'XXX' # dummy "hashed" password
        c.execute("INSERT INTO session_attribute (sid,authenticated,name,value)"
                  " VALUES (%s, 1, 'password', %s)", (user, hashed_password))
        c.execute("INSERT INTO session_attribute (sid,authenticated,name,value)"
                   " VALUES (%s, 1, 'name', %s)",
                  (user, name))
        c.execute("INSERT INTO session_attribute (sid,authenticated,name,value)"
                   " VALUES (%s, 1, 'email', %s)",
                  (user, user + "@users.sourceforge.net"))
        c.execute("INSERT INTO session (sid, authenticated, last_visit)"
                  " VALUES (%s, 1, 0)",
                  (user,))
    db.db().commit()


def reset_all_passwords():
    """Generate random password for all the users and send e-mail to everyone.
       To customize messages, change a template reset_password_email.txt.

       Use with care! Test before using (e.g. with fakemail).
    """
    from trac.core import ComponentMeta

    env = Environment(TRAC_ENV)

    def component_instance(name):
        for i in ComponentMeta._components:
            if i.__name__ == name:
                return i(env)
        raise ValueError("Component not found: " + name)

    mgr = component_instance("AccountManager")
    am = component_instance("AccountModule")

    class Req(): 
        def __init__(self, **kw):
            self.authname = None
            self.method = 'POST'
            self.args = kw

    users = [i for i in mgr.get_users()]
    for sid in users:
        print "sending new password for " + sid
        req = Req(username=sid, email=sid+"@users.sourceforge.net")
        am._do_reset_password(req)



def convert(db, tree):
    project_group_id = tree.find("export_details").findtext("project_group_id")

    if USERS_FILE:
        write_users_file(tree)

    if HANDLE_ATTACHMENTS:
        if not os.path.isdir(CACHE_DIR):
            os.mkdir(CACHE_DIR)

    numbers_file = None
    if NUMBERS_FILE:
        numbers_file = file(NUMBERS_FILE, "w")
        numbers_file.write("||SourceForge||Trac||\n")

    
    trackers = tree.find("trackers")
    for tracker in trackers.findall("tracker"):
        sft = SfTrackerInfo(tracker)
        if sft.name not in IMPORT_SF_TRACKERS:
            print "---- Skipped tracker '%s' ----" % sft.name
            continue

        print "==== Handling tracker '%s' ====" % sft.name
        numitems = len(tracker.find('tracker_items').findall('tracker_item'))
        i = 1
        for item in tracker.find('tracker_items').findall('tracker_item'):
            r = add_item(db, item, sft, numbers_file)
            id = item.findtext('id')
            print "  item %s [%d/%d] -> #%d" % (id, i, numitems, r)
            i += 1


def get_all_involved_users(item):
    def generate_users(item):
        yield item.findtext("submitter") 
        yield item.findtext("assignee")
        for i in item.find("followups").findall("followup"):
            yield i.findtext("submitter")
        for i in item.find("history_entries").findall("history_entry"):
            yield i.findtext("updator")
    cc_list = []
    for i in generate_users(item):
        if i not in cc_list and i != "nobody" and i != "sf-robot":
            cc_list.append(i)
    return cc_list


def add_item(db, item, sft, numbers_file):
    c = db.db().cursor()

    type = sft.type
    opentime = item.findtext("submit_date")

    changetime = opentime
    for i in item.find("followups").findall("followup"):
        date = i.findtext("date")
        if int(date) > int(changetime):
            changetime = date
    for i in item.find("history_entries").findall("history_entry"):
        date = i.findtext("date")
        if int(date) > int(changetime):
            changetime = date


    sf_component = sft.categories[item.findtext("category_id")]
    component = CATEGORY_TO_COMPONENT[sf_component]

    sf_priority = int(item.findtext("priority"))
    priority = PRIORITY_MAP[sf_priority]

    owner = item.findtext("assignee")
    if owner == "nobody":
        owner = ""

    reporter = item.findtext("submitter")
    if reporter == "nobody":
        reporter = "anonymous"

    if CC_EVERYBODY:
        cc = ", ".join(get_all_involved_users(item))
    else:
        cc = ""

    # no version or milestone in SF tracker
    version = ''
    milestone = ''
    
    sf_status = sft.statuses[item.findtext("status_id")]
    status = STATUS_MAP[sf_status]
    if status == "new-or-assigned":
        if owner:
            status = "assigned"
        else:
            status = "new"

    rtext = item.findtext("resolution")
    if rtext:
        resolution_sf = sft.resolutions[rtext].lower()
        resolution = RESOLUTIONS_MAP[resolution_sf]
    else:
        resolution = ""

    summary = item.findtext("summary")

    description = item.findtext("details")
    if PREFORMAT_COMMENTS:
        description = "{{{\n%s\n}}}" % description

    keywords = []
    if sft.name in TRACKER_TO_KEYWORD:
        keywords.append(TRACKER_TO_KEYWORD[sft.name])
    if sf_component in CATEGORY_TO_KEYWORD:
        keywords.append(CATEGORY_TO_KEYWORD[sf_component])

    c.execute("""INSERT INTO ticket (type, time, changetime, component,
                                     priority, owner, reporter, cc,
                                     version, milestone, status, resolution,
                                     summary, description, keywords)
                             VALUES (%s, %s, %s, %s, 
                                     %s, %s, %s, %s,
                                     %s, %s, %s, %s,
                                     %s, %s, %s)""",
              (type, opentime, changetime, component,
              priority, owner, reporter, cc,
              version, milestone, status, resolution,
              summary, description, " ".join(keywords)))

    id = db.db().get_last_id(c, 'ticket')

    if USE_PATCH_CHECKBOX: 
        if sft.name == "Patches":
            value = 1
        else:
            value = 0
        c.execute("""INSERT INTO ticket_custom (ticket, name, value) 
                     VALUES (%s, 'patch', %s)""",
                  (id, value))

    # add comments
    prev_comment_time = None
    for fu in item.find("followups").findall("followup"):
        author = fu.findtext("submitter")
        if author == "nobody":
            author = "anonymous"

        comment_time = fu.findtext("date")
        # the same time (or maybe an error in backup file) is causing problems
        if comment_time == prev_comment_time:
            comment_time = str(int(comment_time) + 1)

        comment = fu.findtext("details")
        junk = comment_junk.search(comment)
        if junk:
            comment = comment[junk.end():]
        if PREFORMAT_COMMENTS:
            comment = '{{{\n%s\n}}}' % comment

        c.execute("""INSERT INTO ticket_change (ticket, time, author, field,
                                                oldvalue, newvalue)
                                        VALUES (%s, %s, %s, %s, %s, %s)""",
                  (id, comment_time, author, 'comment', '', comment))
        prev_comment_time = comment_time

    db.db().commit()

    aid = item.findtext("id")

    if HANDLE_ATTACHMENTS:
        # add attachments
        for a in item.find("attachments").findall("attachment"):
            url = a.findtext("url") + aid
            date = a.findtext("date")
            author = a.findtext("submitter")
            #filetype = a.findtext("filetype")
            filesize = int(a.findtext("filesize"))
            file_id = a.findtext("id")

            filename = a.findtext("filename")
            #problem: <filename>/usr/local/src/wx/DIFF/config.diff</filename>
            if '/' in filename:
                filename = filename.split("/")[-1]

            description = a.findtext("description")

            cachefilename = os.path.join(CACHE_DIR, "%s.dat" % file_id)
            if not os.path.exists(cachefilename):
                downloadfile(url, cachefilename)

            datafile = open(cachefilename, 'rb')
            db.addAttachment(ticket_id=id, filename=filename, datafile=datafile,
                             filesize=filesize, author=author, 
                             description=description, upload_time=date)

    if numbers_file:
        numbers_file.write(
          "||[http://sourceforge.net/support/tracker.php?aid=%s %s]||#%s||\n" 
                           % (aid, aid, id))
    return id



def main():
    print "Parsing XML file `%s'..." % SF_EXPORT2
    tree = ET.parse(SF_EXPORT2)

    print "Trac SQLite('%s'): connecting..." % TRAC_ENV
    db = TracDatabase(TRAC_ENV)

    if TRAC_CLEAN:
        db.clean()
        initialize(db)

    convert(db, tree)


if __name__ == '__main__':
    if len(sys.argv) == 1:
        main()
    elif len(sys.argv) == 2 and sys.argv[1] == "-u":
        db = TracDatabase(TRAC_ENV)
        import_users(db)
    # Be careful. Option -s does mass mailing.
    #elif len(sys.argv) == 2 and sys.argv[1] == "-s":
    #    reset_all_passwords()
    else:
        print "See the usage instruction at the top of the script."

