
import pprint
import os
import sys
import copy
import yaml
from utils import *
import defaults 
from roi_manager import *
from Logger import Logger
from Logger import logLevel as ll

# pylikelihood
from GtApp import GtApp
from BinnedAnalysis import BinnedObs,BinnedAnalysis
from UnbinnedAnalysis import UnbinnedObs, UnbinnedAnalysis
from pyLikelihood import ParameterVector
#from Composite2 import *
from SummedLikelihood import SummedLikelihood
import pyLikelihood as pyLike

from LikelihoodState import LikelihoodState
from UpperLimits import UpperLimits

def filter_dict(d,val):
    for k, v in d.items():
        if v == val: del d[k]


def gtlike_spectrum_to_dict(spectrum):
    """ Convert a pyLikelihood object to a python 
        dictionary which can be easily saved to a file. """
    parameters=ParameterVector()
    spectrum.getParams(parameters)
    d = dict(name = spectrum.genericName())
    for p in parameters: 
        d[p.getName()]= p.getTrueValue()
        d['%s_err' % p.getName()]= p.error()*p.getScale() if p.isFree() else np.nan
        if d['name'] == 'FileFunction': 
            ff=pyLike.FileFunction_cast(spectrum)
            d['file']=ff.filename()
    return d
        

class GTAnalysis(AnalysisBase):
    """High-level analysis interface that internally manages a set of
    analysis component objects.  Most of the interactive functionality
    of the fermiPy package is provided through the methods of this class."""

    defaults = {'common' :
                    dict(defaults.selection.items() +
                         defaults.fileio.items() +
                         defaults.binning.items() +
                         defaults.irfs.items() +
                         defaults.optimizer.items() +
                         defaults.inputs.items(),
                         roi=defaults.roi),
#                'roi' : defaults.roi,
                'verbosity' : (0,'')}

    def __init__(self,config,**kwargs):
        super(GTAnalysis,self).__init__(config,**kwargs)


        pprint.pprint(self.config)
        
        rootdir = os.getcwd()
        
                
        # Destination directory for output data products
        if self.config['common']['base'] is not None:
#            self._savedir = os.path.abspath(config['common']['savedir'])
#        elif config['common']['name'] is not None:
            self._savedir = os.path.join(rootdir,
                                         self.config['common']['base'])
            mkdir(self._savedir)
        else:
            raise Exception('Save directory not defined.')
            
        # Working directory (can be the same as savedir)
        if self.config['common']['scratchdir'] is not None:
            self._workdir = mkdtemp(prefix=os.environ['USER'] + '.',
                                    dir=self.config['common']['scratchdir'])
        else:
            self._workdir = self._savedir


        # put pfiles into savedir
        os.environ['PFILES']=self._savedir+';'+os.environ['PFILES'].split(';')[-1]
            
        self.logger = Logger(os.path.join(self._savedir,
                                          self.config['common']['base']),
                             self.__class__.__name__,
                             ll(self.config['verbosity'])).get()

        # Setup the ROI definition
        self._roi = ROIManager.create_roi_from_source(self.config['common']['target'],
                                                      self.config['common']['roi'])


        self._like = SummedLikelihood()
        self._components = []
        for i,k in enumerate(sorted(config['components'].keys())):

            cfg = self.config['common']
            cfg['roi'] = self.config['common']['roi']
            update_dict(cfg,config['components'][k])

            roi = copy.deepcopy(self._roi)
            roi.configure(cfg['roi'])
            roi.load_diffuse_srcs()
            
            self.logger.info("Creating Analysis Component: " + k)
            comp = GTBinnedAnalysis(cfg,roi,
                                    name=k,
                                    logger=self.logger,
                                    file_suffix='_' + k,
                                    savedir=self._savedir,
                                    workdir=self._workdir)

            self._components.append(comp)
                

    @property
    def like(self):
        return self._like
            
    def create_components(self,analysis_type):
        """Auto-generate a set of components given an analysis type flag."""
        # Lookup a pregenerated config file for the desired analysis setup
        pass

    def setup(self):
        """Run pre-processing step for each analysis component.  This
        will run everything except the likelihood optimization: data
        selection (gtselect, gtmktime), counts maps generation
        (gtbin), model generation (gtexpcube2,gtsrcmaps,gtdiffrsp)."""
        for i, c in enumerate(self._components):

            self.logger.info("Performing setup for Analysis Component: " +
                             c.name)
            c.setup()
            self._like.addComponent(c.like) 
            

    def generate_model(self,model_name=None):
        """Generate model maps for all components."""

        for i, c in enumerate(self._components):
            c.generate_model(model_name=model_name)

        # If all model maps have the same spatial/energy binning we
        # could generate a co-added model map here
            
        
    def free_source(self,name,free=True,skip_pars=['Scale']):
        """Free/Fix all parameters of a source."""

        # Find the source
        if not name in ['isodiff','galdiff','limbdiff']:
            name = self._roi.get_source_by_name(name).name

        # Deduce here the names of all parameters from the spectral type
        parNames = pyLike.StringVector()
        self.like[name].src.spectrum().getParamNames(parNames)

        par_indices = []
        for p in parNames:
            if p in skip_pars: continue            
            par_indices.append(self.like.par_index(name,p))
        
        for idx in par_indices:        
            self.like[idx].setFree(free)
        self.like.syncSrcParams(name)
                
#        freePars = self.like.freePars(name)
#        normPar = self.like.normPar(name).getName()
#        idx = self.like.par_index(name, normPar)
        
#        if not free:
#            self.like.setFreeFlag(name, freePars, False)
#        else:
#            self.like[idx].setFree(True)

        
    def free_norm(self,name,free=True):
        """Free/Fix normalization of a source."""

        normPar = self.like.normPar(name).getName()
        par_index = self.like.par_index(name,normPar)
        self.like[idx].setFree(free)
        self.like.syncSrcParams(name)

    def free_index(self,name,free=True):
        """Free/Fix index of a source."""
        pass

    def initOptimizer(self):
        pass        

    def create_optObject(self):
        """ Make MINUIT or NewMinuit type optimizer object """

        optimizer = self.config['common']['optimizer']
        if optimizer.upper() == 'MINUIT':
            optObject = pyLike.Minuit(self.like.logLike)
        elif optimizer.upper == 'NEWMINUIT':
            optObject = pyLike.NewMinuit(self.like.logLike)
        else:
            optFactory = pyLike.OptimizerFactory_instance()
            optObject = optFactory.create(optimizer, self.like.logLike)
        return optObject
    
    def fit(self):
        """Run likelihood optimization."""

        if not self.like.logLike.getNumFreeParams(): 
            print "Skipping fit.  No free parameters."
            return
        
        saved_state = LikelihoodState(self.like)
        kw = dict(optObject = self.create_optObject(),
                  covar=True,verbosity=0)
#tol=1E-4
#                  optimizer='DRMNFB')
        
#        if 'verbosity' not in kwargs: kwargs['verbosity'] = max(self.config['chatter'] - 1, 0)
        niter = 0; max_niter = self.config['common']['retries']
        try: 
            while niter < max_niter:
                print "Fit iteration:", niter
                niter += 1
                self.like.fit(**kw)
                if isinstance(self.like.optObject,pyLike.Minuit) or \
                        isinstance(self.like.optObject,pyLike.NewMinuit):
                    quality = self.like.optObject.getQuality()
                    if quality > 2: return
                else: return
            raise Exception("Failed to converge with %s"%self.like.optimizer)
        except Exception, message:
            print message
            saved_state.restore()
        

    def fitDRM(self):
        
        kw = dict(optObject = None, #pyLike.Minuit(self.like.logLike),
                  covar=True,#tol=1E-4
                  optimizer='DRMNFB')

        

        
#        self.MIN.tol = float(self.likelihoodConf['mintol'])
        
        
        try:
            self.like.fit(**kw)
        except Exception, message:
            print message
            print "Failed to converge with DRMNFB"

        kw = dict(optObject = pyLike.Minuit(self.like.logLike),
                  covar=True)

        self.like.fit(**kw)
        

    def load_xml(self,xmlfile):
        """Load model definition from XML."""

        
        
        pass

    def write_xml(self,model_name):
        """Save current model definition as XML file.

        Parameters
        ----------

        model_name : str
            Name of the output model.

        """

        for i, c in enumerate(self._components):
            c.write_xml(model_name)

        # Write a common XML file?

    def write_results(self,outfile=None):
        """Write out parameters of current model as yaml file."""
        # extract the results in a convenient format

        if outfile is None:
            outfile = os.path.join(self._savedir,'results.yaml')
        else:
            outfile, ext = os.path.splitext(outfile)
            if not ext:
                outfile = os.path.join(self._savedir,outfile + ext)
            else:
                outfile = outfile + ext
                        
        o = self.get_roi_dict()
                
        # Get the subset of sources with free parameters
            
        yaml.dump(o,open(outfile,'w'))

    def get_roi_dict(self):
        """Populate a dictionary with the parameters of the current ROI model."""
        
        o = {}        
        for name in self.like.sourceNames():
            source = self.like[name].src
            spectrum = source.spectrum()
            o[name] = gtlike_spectrum_to_dict(spectrum)

        return o
        
            
class GTBinnedAnalysis(AnalysisBase):

    defaults = dict(defaults.selection.items()+
                    defaults.binning.items()+
                    defaults.irfs.items()+
                    defaults.inputs.items()+
                    defaults.fileio.items(),
                    roi=defaults.roi,
                    file_suffix=('',''))


    def __init__(self,config,roi,name='binned_analyais',
                 logger=None,**kwargs):
        super(GTBinnedAnalysis,self).__init__(config,**kwargs)

        pprint.pprint(self.config)

        if logger is not None:
            self._logger = logger
        
        savedir = self.config['savedir']
        self._roi = roi
        self._name = name
        
        from os.path import join

        self._ft1_file=join(savedir,
                            'ft1%s.fits'%self.config['file_suffix'])        
        self._ccube_file=join(savedir,
                             'ccube%s.fits'%self.config['file_suffix'])
        self._mcube_file=join(savedir,
                              'mcube%s.fits'%self.config['file_suffix'])
        self._srcmap_file=join(savedir,
                               'srcmap%s.fits'%self.config['file_suffix'])
        self._bexpmap_file=join(savedir,
                                'bexpmap%s.fits'%self.config['file_suffix'])
        self._srcmdl_file=join(savedir,
                               'srcmdl%s.xml'%self.config['file_suffix'])

        self.enumbins = np.round(self.config['binsperdec']*
                                 np.log10(self.config['emax']/self.config['emin']))
        self.enumbins = int(self.enumbins)

        if self.config['npix'] is None:
            self.npix = int(np.round(self.config['roi_width']/self.config['binsz']))
        else:
            self.npix = self.config['npix']
            
    @property
    def roi(self):
        return self._roi

    @property
    def like(self):
        return self._like

    @property
    def name(self):
        return self._name
    
    def setup(self):
        """Run pre-processing step."""

        # Write ROI XML
        self._roi.write_xml(self._srcmdl_file)
        roi_center = self._roi.radec
        
        # Run gtselect
        kw = dict(infile=self.config['evfile'],
                  outfile=self._ft1_file,
                  ra=roi_center[0], dec=roi_center[1],
                  rad=self.config['radius'],
                  evtype=self.config['evtype'],
                  evclass=self.config['evclass'],
                  tmin=self.config['tmin'], tmax=self.config['tmax'],
                  emin=self.config['emin'], emax=self.config['emax'],
                  zmax=self.config['zmax'])
#                  chatter=self.config['chatter'])

        filter_dict(kw,None)
        pprint.pprint(kw)

        if not os.path.isfile(self._ft1_file):
            gtselect=GtApp('gtselect','gtselect')
            gtselect.run(**kw)
        else:
            self._logger.info('Skipping gtselect')
        
        # Run gtmktime

        # Run gtltcube
        if self.config['ltcube'] is not None:
            self._ltcube = self.config['ltcube']
            
        # Run gtbin
        kw = dict(algorithm='ccube',
                  nxpix=self.npix, nypix=self.npix,
                  binsz=self.config['binsz'],
                  evfile=self._ft1_file,
                  outfile=self._ccube_file,
                  scfile=self.config['scfile'],
                  xref=self.roi.radec[0], yref=self.roi.radec[1], axisrot=0,
                  proj=self.config['proj'],
                  ebinalg='LOG', emin=self.config['emin'], emax=self.config['emax'],
                  enumbins=self.enumbins,
                  coordsys=self.config['coordsys'])
#                  chatter=self.config['chatter']

        filter_dict(kw,None)
        pprint.pprint(kw)
        
        if not os.path.isfile(self._ccube_file):
            gtbin=GtApp('gtbin','gtbin')
            gtbin.run(**kw)
        else:
            self._logger.info('Skipping gtbin')

        # Run gtexpcube2
        kw = dict(infile=self._ltcube,cmap='none',
#                  cmap=self._ccube_file,
                  ebinalg='LOG',
                  emin=self.config['emin'], emax=self.config['emax'],
                  enumbins=self.enumbins,
                  outfile=self._bexpmap_file, proj='CAR',
                  nxpix=360, nypix=180, binsz=1,
                  irfs=self.config['irfs'],
                  coordsys=self.config['coordsys'])
#                  chatter=self.config['chatter'])
        
        filter_dict(kw,None)
        pprint.pprint(kw)

        if not os.path.isfile(self._bexpmap_file):
            gtexpcube=GtApp('gtexpcube2','gtexpcube2')
            gtexpcube.run(**kw)
        else:
            print 'Skipping gtexpcube'

        # Run gtsrcmaps
        kw = dict(scfile=self.config['scfile'],
                  expcube=self._ltcube,
                  cmap=self._ccube_file,
                  srcmdl=self._srcmdl_file,
                  bexpmap=self._bexpmap_file,
                  outfile=self._srcmap_file,
                  irfs=self.config['irfs'],
#                   rfactor=self.config['rfactor'],
#                   resample=self.config['resample'],
#                   minbinsz=self.config['minbinsz'],
#                   chatter=self.config['chatter'],
                  emapbnds='no' ) 

        if not os.path.isfile(self._srcmap_file):
            gtsrcmaps=GtApp('gtsrcmaps','gtsrcmaps')
            gtsrcmaps.run(**kw)
        else:
            print 'Skipping gtsrcmaps'

        # Create BinnedObs
        print 'Creating BinnedObs'
        self._obs=BinnedObs(srcMaps=self._srcmap_file,expCube=self._ltcube,
                            binnedExpMap=self._bexpmap_file,irfs=self.config['irfs'])

        # Create BinnedAnalysis
        print 'Creating BinnedAnalysis'
        self._like = BinnedAnalysis(binnedData=self._obs,srcModel=self._srcmdl_file,
                                    optimizer='MINUIT')

        if self.config['enable_edisp']:
            print 'Enabling energy dispersion'
            self.like.logLike.set_edisp_flag(True)
#            os.environ['USE_BL_EDISP'] = 'true'

            
    def generate_model(self,outfile=None,model_name=None):
        """Generate a counts model map.

        Parameters
        ----------

        model_name : str
        
            Name of the model.  If no name is given it will default to
            the seed model.
        """

        if outfile is None: outfile = self._mcube_file
        
        if model_name is None: srcmdl = self._srcmdl_file
        else: srcmdl = self.get_model_path(model_name)
        
        # May consider generating a custom source model file

        if not os.path.isfile(outfile):        
            gtmodel=GtApp('gtmodel')
            gtmodel.run(srcmaps = self._srcmap_file,
                        srcmdl  = srcmdl,
                        bexpmap = self._bexpmap_file,
                        outfile = outfile,
                        expcube = self._ltcube,
                        irfs    = self.config['irfs'],
                        # edisp   = bool(self.config['enable_edisp']),
                        outtype = 'ccube')
#                    chatter=self.config['chatter'],
        else:
            print 'Skipping gtmodel'
            

    def write_xml(self,model_name):
        """Write the XML model for this analysis component."""
        
        xmlfile = self.get_model_path(model_name)            
        print "Writing %s..."%xmlfile
        self.like.writeXml(xmlfile)

    def get_model_path(self,name):
        """Infer the path to the XML model name."""
        
        name, ext = os.path.splitext(name)
        if not ext: ext = '.xml'
        xmlfile = name + self.config['file_suffix'] + ext

        if os.path.commonprefix([self.config['savedir'],xmlfile]) \
                != self.config['savedir']:        
            xmlfile = os.path.join(self.config['savedir'],xmlfile)

        return xmlfile