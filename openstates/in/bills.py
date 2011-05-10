import os
import re
import datetime
from collections import defaultdict

from billy.scrape.bills import BillScraper, Bill
from billy.scrape.votes import Vote
from billy.scrape.utils import convert_pdf

import lxml.html


class INBillScraper(BillScraper):
    state = 'in'

    def scrape(self, chamber, session):
        self.build_subject_mapping(session)

        bill_types = {
            'B': ("http://www.in.gov/apps/lsa/session/billwatch/billinfo"
                  "?year=%s&session=1&request=all" % session),
            'JR': ("http://www.in.gov/apps/lsa/session/billwatch/billinfo?"
                   "year=%s&session=1&request=getJointResolutions" % session),
            'CR': ("http://www.in.gov/apps/lsa/session/billwatch/billinfo?year"
                   "=%s&session=1&request=getConcurrentResolutions" % session),
            'R': ("http://www.in.gov/apps/lsa/session/billwatch/billinfo?year="
                  "%s&session=1&request=getSimpleResolutions" % session)
        }

        for type, url in bill_types.iteritems():
            with self.urlopen(url) as page:
                page = lxml.html.fromstring(page)
                page.make_links_absolute(url)

                abbrev = {'upper': 'S', 'lower': 'H'}[chamber] + type
                xpath = "//a[contains(@href, 'doctype=%s')]" % abbrev
                for link in page.xpath(xpath):
                    bill_id = link.text.strip()
                    self.scrape_bill(session, chamber, bill_id,
                                     link.attrib['href'])

    def scrape_bill(self, session, chamber, bill_id, url):
        if bill_id == 'SCR 0003':
            return

        with self.urlopen(url) as page:
            page = lxml.html.fromstring(page)
            page.make_links_absolute(url)

            title = page.xpath("//br")[8].tail
            if not title:
                return
            title = title.strip()

            abbrev = bill_id.split()[0]
            if abbrev.endswith('B'):
                bill_type = ['bill']
            elif abbrev.endswith('JR'):
                bill_type = ['joint resolution']
            elif abbrev.endswith('CR'):
                bill_type = ['concurrent resolution']
            elif abbrev.endswith('R'):
                bill_type = ['resolution']

            bill = Bill(session, chamber, bill_id, title,
                        type=bill_type)
            bill.add_source(url)

            action_link = page.xpath("//a[contains(@href, 'getActions')]")[0]
            self.scrape_actions(bill, action_link.attrib['href'])

            version_path = "//a[contains(., '%s')]"
            for version_type in ('Introduced Bill', 'House Bill',
                                 'Senate Bill', 'Engrossed Bill',
                                 'Enrolled Act'):
                path = version_path % version_type
                links = page.xpath(path)
                if links:
                    bill.add_version(version_type, links[0].attrib['href'])

            # for vote_link in page.xpath("//a[contains(@href, 'Srollcal')]"):
            #     self.scrape_senate_vote(bill, vote_link.attrib['href'])

            for doc_link in page.xpath("//a[contains(@href, 'FISCAL')]"):
                num = doc_link.text.strip().split("(")[0]
                bill.add_document("Fiscal Impact Statement #%s" % num,
                                  doc_link.attrib['href'])

            bill['subjects'] = self.subjects[bill_id]

            self.save_bill(bill)

    def scrape_actions(self, bill, url):
        with self.urlopen(url) as page:
            page = lxml.html.fromstring(page)

            bill.add_source(url)

            slist = page.xpath("//strong[contains(., 'Authors:')]")[0]
            slist = slist.tail.split(',')
            sponsors = []
            for sponsor in slist:
                name = sponsor.strip()
                if not name:
                    continue
                if name == 'Jr.':
                    sponsors[-1] = sponsors[-1] + ", Jr."
                else:
                    sponsors.append(name)
            for sponsor in sponsors:
                bill.add_sponsor('author', sponsor)

            act_table = page.xpath("//table")[1]
            read_yet = False

            for row in act_table.xpath("tr")[1:]:
                date = row.xpath("string(td[1])").strip()
                date = datetime.datetime.strptime(date, "%m/%d/%Y").date()

                chamber = row.xpath("string(td[2])").strip()
                if chamber == 'S':
                    chamber = 'upper'
                elif chamber == 'H':
                    chamber = 'lower'

                action = row.xpath("string(td[4])").strip(' ;\t\n')

                if not action:
                    # sometimes there are blank actions, just skip these
                    continue

                atype = []

                if action.startswith('First reading:'):
                    if not read_yet:
                        atype.append('bill:introduced')
                        read_yet = True
                    atype.append('bill:reading:1')
                if action.startswith('Second reading:'):
                    atype.append('bill:reading:2')
                if action.startswith('Third reading:'):
                    if action.startswith('Third reading: passed'):
                        atype.append('bill:passed')
                    atype.append('bill:reading:3')
                if 'referred to' in action:
                    atype.append('committee:referred')
                if action.startswith('Referred to Committee'):
                    atype.append('committee:referred')
                if action.startswith('Reassigned to'):
                    atype.append('committee:referred')

                match = re.match(r'Amendment \d+ \(.*\), (prevailed|failed)', action)
                if match:
                    if match.group(1) == 'prevailed':
                        atype.append('amendment:passed')
                    else:
                        atype.append('amendment:failed')

                bill.add_action(chamber, action, date, type=atype)

    def build_subject_mapping(self, session):
        self.subjects = defaultdict(list)

        url = ("http://www.in.gov/apps/lsa/session/billwatch/billinfo"
               "?year=%s&session=1&request=getSubjectList" % session)
        with self.urlopen(url) as page:
            page = lxml.html.fromstring(page)
            page.make_links_absolute(url)

            for link in page.xpath("//a[contains(@href, 'getSubject')]"):
                subject = link.text.strip()

                self.scrape_subject(subject, link.attrib['href'])

    def scrape_subject(self, subject, url):
        with self.urlopen(url) as page:
            page = lxml.html.fromstring(page)

            for link in page.xpath("//a[contains(@href, 'getBill')]"):
                self.subjects[link.text.strip()].append(subject)

    def scrape_senate_vote(self, bill, url):
        (path, resp) = self.urlretrieve(url)
        text = convert_pdf(path, 'text-nolayout')
        os.remove(path)

        lines = text.split('\n')

        date_fmt = "%m/%d/%Y %I:%M:%S %p"
        date, vstart = None, None
        try:
            date = "%s %s" % (lines[-4], lines[-3])
            date = datetime.datetime.strptime(date, date_fmt)
            vstart = 20
        except ValueError:
            try:
                date = "%s %s" % (lines[6], lines[7])
                date = datetime.datetime.strptime(date, date_fmt)
                vstart = 27
            except ValueError:
                self.log("Couldn't find vote date in %s" % url)
                return

        vote_type = None
        yes_count, no_count, other_count = None, None, 0
        votes = []
        for line in lines[vstart:]:
            line = line.strip()
            if not line:
                continue

            if line.startswith('YEAS'):
                yes_count = int(line.split(' - ')[1])
                vote_type = 'yes'
            elif line.startswith('NAYS'):
                no_count = int(line.split(' - ')[1])
                vote_type = 'no'
            elif line.startswith('EXCUSED') or line.startswith('NOT VOTING'):
                other_count += int(line.split(' - ')[1])
                vote_type = 'other'
            else:
                votes.append((line, vote_type))

        if yes_count is None or no_count is None:
            self.log("Couldne't find vote counts in %s" % url)
            return

        passed = yes_count > no_count + other_count
        motion = lines[7].strip()

        vote = Vote('upper', date, motion, passed, yes_count, no_count,
                    other_count)
        vote.add_source(url)

        for name, vtype in votes:
            if vtype == 'yes':
                vote.yes(name)
            elif vtype == 'no':
                vote.no(name)
            elif vtype == 'other':
                vote.other(name)

        bill.add_vote(vote)
