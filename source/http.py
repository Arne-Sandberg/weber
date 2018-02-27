#!/usr/bin/env python3
"""
Class for HTTP is here.
"""
import socket, traceback, errno, ssl, re, itertools, threading
from threading import Thread
from collections import OrderedDict
from select import select

from source import weber
from source import log
from source.proxy import ProxyLib, ConnectionThread
from source.structures import URI
from source.lib import *
from source.fd_debug import *

class HTTP():
    """
    HTTP class generating proper HTTP objects and holding HTTP-specific constants
    """
    
    link_tags = [
        (b'<a', b'</a>', b'href'),
        (b'<form', b'</form>', b'action'),
        (b'<frame', b'</frame>', b'src'),
        (b'<img', b'>', b'src'),
        (b'<script', b'>', b'src'),
        (b'<link', b'>', b'href'),
    ] # TODO more

    scheme = 'http'
    ssl_scheme = 'https'
    port = 80
    ssl_port = 443

    @staticmethod
    def create_connection_thread(conn, rrid, tamper_request, tamper_response, template_rr=None, brute_set=None):
        return HTTPConnectionThread(conn, rrid, tamper_request, tamper_response, template_rr, brute_set)

    @staticmethod
    def create_request(data, should_tamper, no_stopper=False):
        return HTTPRequest(data, should_tamper, no_stopper)
    
    @staticmethod
    def create_response(data, should_tamper, no_stopper=False):
        return HTTPResponse(data, should_tamper, no_stopper)
    
    @staticmethod
    def request_string(req, res, colored=False):
        # response is needed for proper colors

        if True:#try:
            # TODO also from Accept:
            tamperstring = ''
            if req.tampering:
                tamperstring = '[T] '
            color = log.COLOR_NONE
            
            color = log.COLOR_NONE
            # do the coloring
            if colored:
                if not res:
                    # no response received, color by extension
                    if req.onlypath == b'/' or req.onlypath.endswith((b'.htm', b'.html', b'.php', b'.xhtml', b'.aspx')):
                        color = log.MIMECOLOR_HTML
                    elif req.onlypath.endswith((b'.jpg', b'.svg', b'.png', b'.gif', b'.ico')):
                        color = log.MIMECOLOR_IMAGE
                    elif req.onlypath.endswith((b'.mp3', b'.ogg', b'.mp4', b'.wav')):
                        color = log.MIMECOLOR_MULTIMEDIA
                    elif req.onlypath.endswith((b'.js', b'.vbs', b'.swf')):
                        color = log.MIMECOLOR_SCRIPT
                    elif req.onlypath.endswith((b'.css')):
                        color = log.MIMECOLOR_CSS
                    elif req.onlypath.endswith((b'.pdf', b'.doc', b'.docx', b'.xls', b'.xlsx', b'.ppt', b'.pptx', b'.pps', b'.ppsx')):
                        color = log.MIMECOLOR_DOCUMENT
                    elif req.onlypath.endswith(b'.txt'):
                        color = log.MIMECOLOR_PLAINTEXT
                    elif req.onlypath.endswith((b'.zip', b'.7z', b'.rar', b'.gz', b'.bz2', b'.jar', b'.bin', b'.iso')):
                        color = log.MIMECOLOR_ARCHIVE
                else:
                    # response received, color by Content-Type
                    content_type = res.headers.get(b'Content-Type')
                    if content_type: # missing Content-Type will be detected in analysis
                        color = get_color_from_content_type(content_type)


            return '%s%s%s%s%s %s%s' % (log.COLOR_YELLOW, tamperstring, log.COLOR_NONE, color, req.method.decode(), req.path.decode(), log.COLOR_NONE)
        if False:#except:
            return log.COLOR_YELLOW+log.COLOR_NONE+log.COLOR_GREY+'...'+log.COLOR_NONE
    

    @staticmethod
    def response_string(res, colored=False):
        try:
            tamperstring = ''
            if res.tampering:
                tamperstring = '[T] '
            if res.statuscode < 200:
                color = log.COLOR_NONE
            elif res.statuscode == 200:
                color = log.COLOR_DARK_GREEN
            elif res.statuscode < 300:
                color = log.COLOR_GREEN
            elif res.statuscode < 400:
                color = log.COLOR_BROWN
            elif res.statuscode < 500:
                color = log.COLOR_DARK_RED
            elif res.statuscode < 600:
                color = log.COLOR_DARK_PURPLE
            else:
                color = log.COLOR_NONE
            if not colored:
                color = log.COLOR_NONE
            
            return '%s%s%s%s%d %s%s' % (log.COLOR_YELLOW, tamperstring, log.COLOR_NONE, color, res.statuscode, res.status.decode(), log.COLOR_NONE)
        except:
            return log.COLOR_YELLOW+log.COLOR_NONE+log.COLOR_GREY+'...'+log.COLOR_NONE
    

    # analysis stuff
    intra_tests = [
        ('Missing Content-Type', lambda req,res:(('WARNING', 'Content-Type is not defined.') if res and not res.headers.get(b'Content-Type') else None)),
        ('PHP returned', lambda req,res:(('SECURITY', 'PHP code returned from server.') if res and is_content_type_text(res.headers.get(b'Content-Type')) and res.find_tags(startends=[(b'<?', b'?>')], valueonly=False) else None)),
    ]


weber.protocols['http'] = HTTP




class HTTPConnectionThread(ConnectionThread):
    """
    Class for dealing with HTTP communication.
    Most stuff is done in parent generic ConnectionThread (source.proxy).
    """
    def __init__(self, conn, rrid, tamper_request, tamper_response, template_rr=None, brute_set=None):
        super().__init__(conn, rrid, tamper_request, tamper_response, template_rr, brute_set)
        self.Protocol = HTTP
        log.debug_socket('HTTP ConnectionThread created.')
        # conn - socket to browser, None if from template
        # rrid - index of request-response pair
        # tamper_request - should the request forwarding be delayed?
        # tamper_response - should the response forwarding be delayed?
        # known_rr - known request (e.g. copy of existing for bruteforcing) - don't communicate with browser if not None
        # brute_set - list of values destined for brute placeholder replacing

    
    def run(self):
        request = None
        response = None
        while self.keepalive:
            # receive request from browser / copy from template RR
            if self.template_rr is None:
                request = self.receive_request()
            else:
                request = self.template_rr.request_upstream.clone(self.tamper_request)
            
            if request is None: # socket closed? socket problem?
                log.debug_parsing('Request is broken, ignoring...') # TODO comment, 
                break
            self.keepalive = (request.headers.get(b'Connection') == b'Keep-Alive')
            
            # get URI() from request
            downstream_referer = None
            if self.template_rr is None:
                try:
                    self.host, _, port = request.headers[b'Host'].partition(b':')
                    self.port = int(port)
                except:
                    self.host = b''
                    self.port = self.Protocol.port
                self.localuri = URI(URI.build_str(self.host, self.port, request.path))
                if request.headers.get(b'Referer'):
                    downstream_referer = URI(request.headers.get(b'Referer'))
            else:
                self.localuri = self.template_rr.uri_upstream.clone()

            # localuri had problems in the past? give up...

            if str(self.localuri) in weber.forward_fail_uris:
                break

            log.debug_mapping('request source: %s ' % (str(self.localuri)))
            log.debug_parsing('\n'+'-'*15+'\n'+str(request)+'\n'+'='*20)
            self.path = request.path.decode()
            
            # create request backup, move into RRDB
            request_downstream = request.clone()
            request.sanitize()
            weber.rrdb.add_request(self.rrid, request_downstream, request, HTTP)
            weber.rrdb.rrs[self.rrid].uri_downstream = self.localuri
            
            
            # change outgoing links (useless if from template)
            if self.template_rr is None:
                self.remoteuri = weber.mapping.get_remote(self.localuri)
                if self.remoteuri is None:
                    log.err('Cannot forward - local URI is not mapped. Terminating thread...')
                    weber.forward_fail_uris.append(str(self.localuri))
                    break
                upstream_referer = weber.mapping.get_remote(downstream_referer)

                request.path = self.remoteuri.path.encode()
                request.parse_method()
                request.headers[b'Host'] = self.remoteuri.domain.encode() if self.remoteuri.port in [self.Protocol.port, self.Protocol.ssl_port] else b'%s:%d' % (self.remoteuri.domain.encode(), self.remoteuri.port)
                if upstream_referer:
                    request.headers[b'Referer'] = upstream_referer.get_value().encode()
                log.debug_parsing('\n'+str(request)+'\n'+'#'*20)
            else:
                self.remoteuri = self.localuri.clone() # as we are working with upstream rr already
            
            weber.rrdb.rrs[self.rrid].uri_upstream = self.remoteuri
            
            # change brute placeholders
            if self.brute_set is not None:
                brute_bytes = request.bytes()
                placeholder = weber.config['brute.placeholder'][0].encode()
                for i in range(len(self.brute_set)):
                    brute_bytes = brute_bytes.replace(b'%s%d%s' % (placeholder, i, placeholder), self.brute_set[i])
                request.parse(brute_bytes)


            # tamper request
            if request.tampering and positive(weber.config['overview.realtime'][0]):
                log.tprint('\n'.join(weber.rrdb.overview(['%d' % self.rrid], header=False)))
            r, _, _ = select([request.forward_stopper[0], self.stopper[0]], [], [])
            if self.stopper[0] in r:
                # Weber is terminating
                break
            
            # forward request to server        
            log.debug_socket('Forwarding request... (%d B)' % (len(request.data)))
            response = self.forward(self.remoteuri, request.bytes())
            
            if response is None:
                weber.forward_fail_uris.append(str(self.localuri))
                break
            ###############################################################################

            log.debug_parsing('\n'+str(response)+'\n'+'='*30)
            
            # move response into RRDB
            response.sanitize()
            weber.rrdb.add_response(self.rrid, response, None, allow_analysis=False)

            
            # tamper response
            if response.tampering and positive(weber.config['overview.realtime'][0]):
                log.tprint('\n'.join(weber.rrdb.overview(['%d' % self.rrid], header=False)))
            r, _, _ = select([response.forward_stopper[0], self.stopper[0]], [], [])
            if self.stopper[0] in r:
                # Weber is terminating
                break


            # spoof files if desired (with or without GET arguments)
            spoof_path = self.remoteuri.get_value() if positive(weber.config['spoof.arguments'][0]) else self.remoteuri.get_value().partition('?')[0]
            if spoof_path in weber.spoof_files.keys():
                response.spoof(weber.spoof_files[spoof_path])


            # set response as downstream, create backup (upstream), update RRDB and do the analysis
            response_upstream = response.clone()
            response_upstream.tampering = False
            weber.rrdb.add_response(self.rrid, response_upstream, response, allow_analysis=True)
            

            # alter redirects, useless if from template # TODO test 302, 303, # TODO more?
            if self.template_rr is None:
                if response.statuscode in [301, 302, 303]:
                    location = response.headers[b'Location']
                    if location.startswith((b'http://', b'https://')): # absolute redirect
                        newremote = URI(response.headers[b'Location'])
                        newlocal = weber.mapping.get_local(newremote)
                        response.headers[b'Location'] = newlocal.__bytes__()
                    else: # relative redirect
                        pass
                    # no permanent redirection # TODO for which 3xx's?
                    response.statuscode = 302

                # change incoming links, useless if from template
                for starttag, endtag, attr in HTTP.link_tags:
                    response.replace_links(starttag, endtag, attr)

                log.debug_parsing('\n'+str(response)+'\n'+'-'*30)

            # send response to browser if not from template
            if self.template_rr is None:
                try:
                    self.send_response(response)
                except socket.error as e:
                    if isinstance(e.args, tuple):
                        if e.args[0] == errno.EPIPE:
                            log.err('Connection closed for #%d, response not forwarded.' % (self.rrid))
                        else:
                            raise e
                    else:
                        raise e
                except Exception as e:
                    log.err('Failed to forward response (#%d): %s' % (self.rrid, str(e)))
                    log.err('See traceback:')
                    traceback.print_exc()

            # print if desired
            if positive(weber.config['overview.realtime'][0]):
                log.tprint('\n'.join(weber.rrdb.overview(['%d' % self.rrid], header=False)))
        
        # close connection if not None (from template)
        if self.conn:
            self.conn.close()

        # close stoppers
        
        for r in [request, response]:
            if r:
                for fd in [0, 1]:
                    if r.forward_stopper:
                        os.close(r.forward_stopper[fd])
                r.forward_stopper = None
        for fd in [0, 1]:
            if self.stopper:
                os.close(self.stopper[fd])
        self.stopper = None
        



 







class HTTPRequest():
    """
    HTTP Request class
    """
    def __init__(self, data, should_tamper, no_stopper=False):
        """
            data = request data (bytes)
            should_tamper = should the request be tampered? (bool)
        """
        self.integrity = False
        if not data:
            return
        
        # set up tampering mechanism
        self.should_tamper = should_tamper
        #self.forward_stopper = None if no_stopper else os.pipe()
        self.forward_stopper = os.pipe()
        self.tampering = self.should_tamper
        
        self.onlypath = '' 

        # parse data
        self.parse(data)

        # allow forwarding immediately?
        if not self.should_tamper:
            self.forward()


    def parse(self, data):
        # parse given bytes (from socket, editor, file, ...)
        self.original = data
        lines = data.splitlines()
        self.method, self.path, self.version = tuple(lines[0].split(b' '))
        fd_add_comment(self.forward_stopper, 'Request (%s %s) forward stopper' % (self.method, self.path))
        self.parameters = {}
        
        self.headers = OrderedDict()
        self.data = b''
        for line in lines[1:-1]:
            if not line:
                continue
            k, _, v = line.partition(b':')
            # TODO duplicit keys? warn
            self.headers[k] = v.strip()
           
        if len(lines[-1]) > 0:
            self.data = lines[-1]

        self.parse_method()
        self.integrity = True

    
    def sanitize(self):
        # alter the Request so we don't have to deal with problematic options, e.g. encoding
        # should not be used on the original (downstream) Request

        # disable encoding
        self.headers.pop(b'Accept-Encoding', None)
        # disable Range
        self.headers.pop(b'Range', None)
        self.headers.pop(b'If_Range', None)


    def clone(self, should_tamper=False, no_stopper=True):
        return HTTP.create_request(self.bytes(), should_tamper, no_stopper) 

    def forward(self):
        self.tampering = False
        if self.forward_stopper:
            os.write(self.forward_stopper[1], b'1')

    def parse_method(self):
        # GET, HEAD method
        if self.method in [b'GET', b'HEAD']:
            self.onlypath, _, tmpparams = self.path.partition(b'?')
            for param in tmpparams.split(b'&'):
                if param == b'':
                    continue
                k, _, v = tuple(param.partition(b'='))
                v = None if v == b'' else v
                self.parameters[k] = v
        # POST method
        if self.method in [b'POST']:
            self.onlypath, _, _ = self.path.partition(b'?')
            for param in self.data.split(b'&'):
                if param == b'':
                    continue
                k, _, v = param.partition(b'=')
                v = None if v == b'' else v
                self.parameters[k] = v
        # TODO more methods



    def lines(self, headers=True, data=True, as_string=True):
        parts = []
        
        if headers:
            parts.append(b'%s %s %s' % (self.method, self.path, self.version))
            parts += [b'%s: %s' % (k, '' if v is None else v) for k, v in self.headers.items()]
            if data:
                parts.append(b'')
        if data:
            parts += self.data.split(b'\n')
        try:
            parts = [x.decode() for x in parts] if as_string else parts
        except Exception as e:
            log.warn('Response encoding problem occured: '+str(e))
            parts = []
        return parts
        

    def __str__(self):
        return '\n'.join(self.lines())

    def bytes(self):
        result = b'%s %s %s\r\n' % (self.method, self.path, self.version)
        result += b'\r\n'.join([b'%s: %s' % (k, b'' if v is None else v) for k, v in self.headers.items()])
        result += b'\r\n\r\n'
        if len(self.data)>0:
            result += self.data
        return result








class HTTPResponse():

    def __init__(self, data, should_tamper, no_stopper=False):
        # set up tampering mechanism
        self.should_tamper = should_tamper
        self.forward_stopper = None if no_stopper else os.pipe()
        self.tampering = should_tamper
        
        
        # parse data
        self.parse(data)

        
        # allow forwarding?
        if not self.should_tamper:
            self.forward()
    
    @staticmethod
    def spoof_regex(data):
        for old, new in weber.spoof_regexs.items():
            data = re.sub(old.encode(), new.encode(), data)
        return data

    def parse(self, data):
        # parse given bytes (from socket, editor, file, ...)
        self.original = data
        lines = data.split(b'\r\n')

        line0 = ProxyLib.spoof_regex(lines[0])

        self.version = line0.partition(b' ')[0]
        try:
            self.statuscode = int(line0.split(b' ')[1])
        except:
            log.warn('Non-integer status code received.')
            self.statuscode = 0
        self.status = b' '.join(line0.split(b' ')[2:])
        fd_add_comment(self.forward_stopper, 'Response (%d %s) forward stopper' % (self.statuscode, self.status))
        
        self.headers = OrderedDict()

        # load first set of headers (hopefully only one)
        line_index = 1
        for line_index in range(1, len(lines)):
            line = ProxyLib.spoof_regex(lines[line_index])
            if len(line) == 0:
                break
            k, _, v = line.partition(b':')
            self.headers[k] = v.strip()
        
        line_index += 1

        # chunked Transfer-Encoding?
        data_line_index = line_index # backup the value
        try:
            self.data = b''
            while True: # read all chunks
                log.debug_chunks('trying to unchunk next chunk...')
                log.debug_chunks('next line: %s' % (str(lines[line_index])))
                chunksize = int(lines[line_index], 16)
                log.debug_chunks('chunksize (parsed): 0x%x' % (chunksize))
                if chunksize == 0: # end of stream
                    log.debug_chunks('unchunking finished.')
                    break
                tmpchunk = b''
                while True: # read all bytes for chunk
                    line_index += 1
                    tmpchunk += lines[line_index]
                    if len(tmpchunk) == chunksize: # chunk is complete
                        log.debug_chunks('end of chunk near %s' % str(lines[line_index][-30:]))
                        line_index += 1
                        break
                    if len(tmpchunk) > chunksize: # problem...
                        log.warn('Loaded chunk is bigger than advertised: %d > %d' % (len(tmpchunk), chunksize))
                        break
                    # chunk spans multiple lines...
                    tmpchunk += b'\r\n'
                self.data += ProxyLib.spoof_regex(tmpchunk)
        except Exception as e:
            line_index = data_line_index # restore the value
            log.debug_chunks('unchunking failed:')
            log.debug_chunks(e)
            #traceback.print_exc()
            log.debug_chunks('treating as non-chunked...')
            # treat as normal data
            self.data = ProxyLib.spoof_regex(b'\r\n'.join(lines[line_index:]))
            # TODO test for matching Content-Type (HTTP Response-Splitting etc.)
        
    
    def sanitize(self):
        # alter the Response so we don't have to deal with problematic options, e.g. chunked
        # should NOT be used on the original (upstream) Response
        
        # strip Transfer-Encoding...
        self.headers.pop(b'Transfer-Encoding', None)
        
        # no wild upgrading (HTTP/2)
        self.headers.pop(b'Upgrade', None)


    def clone(self, should_tamper=True, no_stopper=True):
        return HTTP.create_response(self.bytes(), should_tamper, no_stopper)

    def forward(self):
        self.tampering = False
        if self.forward_stopper:
            os.write(self.forward_stopper[1], b'1')
 
    def compute_content_length(self):
        #if b'Content-Length' not in self.headers.keys() and len(self.data)>0:
        log.debug_parsing('Computing Content-Length...')
        self.headers[b'Content-Length'] = b'%d' % (len(self.data))

    def lines(self, headers=True, data=True, as_string=True):
        parts = []
        if headers:
            self.compute_content_length()
            parts.append(b'%s %d %s' % (self.version, self.statuscode, self.status))
            parts += [b'%s: %s' % (k, '' if v is None else v) for k, v in self.headers.items()]
            if data:
                parts.append(b'')
        if data:
            # Do not include if string is desired and it is binary content
            # TODO more Content-Types
            if as_string and self.statuscode < 300 and not is_content_type_text(self.headers.get(b'Content-Type')):
                parts.append(b'--- BINARY DATA ---')
            else:
                parts += self.data.split(b'\n')
            
        try:
            parts = [x.decode('utf-8', 'replace') for x in parts] if as_string else parts # not accurate
        except Exception as e:
            log.warn('Response encoding problem occured: %s' % (str(e)))
            log.warn('For '+str(self.headers))
            parts = []
        return parts

    def __str__(self):
        return '\n'.join(self.lines())

    def bytes(self):
        self.compute_content_length()
        #data = lib.gzip(self.data) if self.headers.get(b'Content-Encoding') == b'gzip'  else self.data
        result = b''
        result += b'%s %d %s\r\n' % (self.version, self.statuscode, self.status)
        result += b'\r\n'.join([b'%s: %s' % (k, b'' if v is None else v) for k, v in self.headers.items()])
        
        result += b'\r\n\r\n' + self.data + b'\r\n\r\n'
        return result
    
    def find_html_attr(self, tagstart, tagend, attr):
        # this method uses find_between() method to locate attributes and their values for specified tag
        # returns list of (absolute_position, match_string) of attributes
        tagmatches = find_between(self.data, tagstart, tagend)
        result = []
        for pos, _ in tagmatches:
            # find the end of the tagstart
            endpos = self.data.index(b'>', pos)
            linkmatches = find_between(self.data, b'%s="' % (attr), b'"', startpos=pos, endpos=endpos, inner=True)
            #if not linkmatches: # try without '"' # TODO should be done, but how to get good loffset in self.replace_links()?
            #    linkmatches = find_between(self.data, b'%s=' % (attr), b' ', startpos=pos, endpos=endpos, inner=True)
            result += linkmatches
        return result

    def replace_links(self, tagstart, tagend, attr):
        # this method searches desired tag attributes using find_html_attr() and replaces its content
        # result is directly written into self.data
        oldparts = [] # unchanged HTML chunks
        loffset = 0
        linkmatches = self.find_html_attr(tagstart, tagend, attr)
        for roffset, value in linkmatches:
            # add chunk until match
            oldparts.append(self.data[loffset:roffset])
            # prepare for new chunk
            loffset = roffset+len(attr) + 2 + len(value) + 1
            #                  href       =" index.html    "
        # add last chunk
        oldparts.append(self.data[loffset:])

        # get new values if desired
        newparts = [b'%s="%s"' % (attr, (x[1] if not x[1].partition(b'://')[0] in (b'http', b'https') else weber.mapping.get_local(x[1]))) for x in linkmatches]
        # join oldparts and newparts
        result = filter(None, [x for x in itertools.chain.from_iterable(itertools.zip_longest(oldparts, newparts))])
        self.data = b''.join(result)


    def find_tags(self, startends, attrs=None, valueonly=False):
        result = []
        if attrs is None:
            for startbytes, endbytes in startends:
                result += [x[1].decode() for x in find_between(self.data, startbytes, endbytes, inner=valueonly)]
        else:
            for (startbytes, endbytes), attr in zip(startends, attrs):
                result += [x[1].decode() for x in self.find_html_attr(startbytes, endbytes, attr)]
        return result


    def spoof(self, path):
        # replace data with file content
        try:
            with open(path, 'rb') as f:
                self.data = f.read()
            self.statuscode = 200
            self.status = b'OK'
            self.compute_content_length()
        except:
            log.err('Spoofing failed - cannot open file.')


