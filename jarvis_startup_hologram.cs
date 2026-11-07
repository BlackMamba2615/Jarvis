// Procedural 3D armor, drawn into a per-pixel alpha window. No raster assets,
// web renderer, text, audio, credentials or microphone are used by this file.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal struct HoloVector
{
    internal double X, Y, Z;
    internal HoloVector(double x, double y, double z) { X = x; Y = y; Z = z; }
    public static HoloVector operator +(HoloVector a, HoloVector b) { return new HoloVector(a.X+b.X, a.Y+b.Y, a.Z+b.Z); }
    public static HoloVector operator -(HoloVector a, HoloVector b) { return new HoloVector(a.X-b.X, a.Y-b.Y, a.Z-b.Z); }
    public static HoloVector operator *(HoloVector a, double s) { return new HoloVector(a.X*s, a.Y*s, a.Z*s); }
    internal static HoloVector Rotate(HoloVector v, double yaw, double pitch, double roll)
    {
        double x = v.X*Math.Cos(yaw)+v.Z*Math.Sin(yaw);
        double z = -v.X*Math.Sin(yaw)+v.Z*Math.Cos(yaw);
        double y = v.Y*Math.Cos(pitch)-z*Math.Sin(pitch);
        z = v.Y*Math.Sin(pitch)+z*Math.Cos(pitch);
        return new HoloVector(x*Math.Cos(roll)-y*Math.Sin(roll), x*Math.Sin(roll)+y*Math.Cos(roll), z);
    }
    internal static HoloVector Cross(HoloVector a, HoloVector b)
    { return new HoloVector(a.Y*b.Z-a.Z*b.Y, a.Z*b.X-a.X*b.Z, a.X*b.Y-a.Y*b.X); }
    internal double Length { get { return Math.Sqrt(X*X+Y*Y+Z*Z); } }
}

internal sealed class HoloPart
{
    internal HoloVector Center, Offset;
    internal double Start, Duration, Roll, Yaw;
    internal readonly List<HoloFace> Faces = new List<HoloFace>();
    internal double Progress(double time) { return HologramRenderer.Clamp((time-Start)/Duration); }
    internal HoloVector Move(HoloVector vertex, double time)
    {
        double p = Progress(time), e = 1-Math.Pow(1-p, 3);
        HoloVector local = HoloVector.Rotate(vertex-Center, Yaw*(1-e), 0.2*(1-e), Roll*(1-e));
        return Center+local*(0.8+0.2*e)+Offset*(1-e);
    }
}

internal sealed class HoloFace
{
    internal HoloVector[] Vertices;
    internal Color Color;
    internal int Seed;
    internal HoloFace(HoloVector[] vertices, Color color, int seed) { Vertices=vertices; Color=color; Seed=seed; }
}

internal sealed class HoloDrawFace
{
    internal PointF[] Points;
    internal double Depth, Light, Alpha, Progress, Y;
    internal Color Color;
    internal int Seed;
}

internal sealed class HoloLight
{
    internal HoloPart Part;
    internal HoloVector[] Polygon;
    internal HoloLight(HoloPart part, HoloVector[] polygon) { Part=part; Polygon=polygon; }
}

internal sealed class HologramRenderer
{
    private readonly List<HoloPart> parts = new List<HoloPart>();
    private readonly List<HoloLight> lights = new List<HoloLight>();
    private HoloPart chest;
    private int seed;
    private static readonly Color Red = Color.FromArgb(162, 25, 34);
    private static readonly Color Scarlet = Color.FromArgb(211, 45, 48);
    private static readonly Color Gold = Color.FromArgb(216, 161, 73);
    private static readonly Color PaleGold = Color.FromArgb(246, 202, 116);
    private static readonly Color Dark = Color.FromArgb(29, 39, 49);
    private static readonly Color Steel = Color.FromArgb(85, 109, 125);

    internal HologramRenderer() { BuildArmor(); }
    internal static double Clamp(double value) { return Math.Max(0, Math.Min(1, value)); }
    private static double Smooth(double value) { double p=Clamp(value); return p*p*(3-2*p); }
    private static HoloVector V(double x, double y, double z) { return new HoloVector(x,y,z); }

    private HoloPart Part(double x, double y, double z, double start, double dx, double dy, double dz, double roll)
    {
        HoloPart part = new HoloPart { Center=V(x,y,z), Start=start, Duration=1.25,
            Offset=V(dx,dy,dz), Roll=roll, Yaw=dx*0.7 };
        parts.Add(part); return part;
    }

    // A beveled extrusion, not a flat sprite. Subdivided facets catch moving
    // lights individually and materialize on the actual armor surfaces.
    private void Plate(HoloPart part, double[] xy, double front, double thickness, Color color)
    {
        int count=xy.Length/2;
        HoloVector center=V(0,0,front+0.045);
        for (int i=0; i<count; i++) { center.X+=xy[i*2]/count; center.Y+=xy[i*2+1]/count; }
        HoloVector[] vertices=new HoloVector[count];
        for (int i=0; i<count; i++) vertices[i]=V(xy[i*2],xy[i*2+1],front);
        for (int i=0; i<count; i++)
        {
            HoloVector a=vertices[i], b=vertices[(i+1)%count];
            HoloVector ab=(a+b)*0.5, ac=(a+center)*0.5, bc=(b+center)*0.5;
            Face(part,color,a,ab,ac); Face(part,color,ab,b,bc);
            Face(part,color,ac,bc,center); Face(part,color,ab,bc,ac);
            HoloVector backA=a-V(0,0,thickness), backB=b-V(0,0,thickness);
            Face(part,color,a,backA,backB); Face(part,color,a,backB,b);
        }
    }
    private void Face(HoloPart part, Color color, params HoloVector[] vertices)
    { part.Faces.Add(new HoloFace(vertices,color,seed++)); }
    private static double[] Mirror(double[] points, int side)
    {
        double[] output=(double[])points.Clone();
        for(int i=0;i<output.Length;i+=2) output[i]*=side;
        return output;
    }

    private void BuildArmor()
    {
        // A tall, anatomically tapered silhouette with red outer shells and
        // inset gold mechanics. Each group has a distinct assembly trajectory.
        HoloPart spine=Part(0,3.65,-0.15,0.10,0,-0.55,-0.6,0);
        Plate(spine,new double[]{-.34,4.6,.34,4.6,.43,3.4,.25,2.9,-.25,2.9,-.43,3.4},-.05,.4,Dark);
        chest=Part(0,4.45,.15,0.55,0,-.65,1.0,0);
        Plate(chest,new double[]{-.35,5.15,.35,5.15,.89,4.86,.78,4.37,.50,4.03,-.5,4.03,-.78,4.37,-.89,4.86},.34,.56,Red);
        Plate(chest,new double[]{-.33,5.08,.33,5.08,.40,4.94,-.4,4.94},.36,.07,Steel);
        for(int side=-1;side<=1;side+=2)
        {
            Plate(chest,Mirror(new double[]{.05,4.88,.65,4.98,.82,4.77,.60,4.53,.13,4.56},side),.43,.08,Scarlet);
            Plate(chest,Mirror(new double[]{.14,4.49,.59,4.48,.72,4.32,.43,4.15,.17,4.22},side),.42,.07,Red);
            Plate(chest,Mirror(new double[]{.57,4.25,.76,4.44,.78,3.88,.43,3.55,.42,3.96},side),.13,.26,Gold);
            Plate(chest,Mirror(new double[]{.56,4.19,.68,4.31,.65,3.91,.48,3.74},side),.22,.04,Dark);
            for(int rib=0;rib<3;rib++)
                Plate(chest,Mirror(new double[]{.48,4.10-rib*.12,.69,4.24-rib*.12,.67,4.18-rib*.12,.47,4.04-rib*.12},side),.25,.04,Steel);
        }
        for(int row=0;row<3;row++)
        {
            double y=3.99-row*.25, w=.43-row*.025;
            HoloPart abdomen=Part(0,y,.15,1.0+row*.14,0,-.45,.9,0);
            Plate(abdomen,new double[]{-w,y,w,y,w*.88,y-.20,0,y-.25,-w*.88,y-.20},.32,.32,row==1?Scarlet:Red);
            Plate(abdomen,new double[]{-w*.80,y-.04,0,y-.10,w*.8,y-.04,w*.7,y-.10,0,y-.17,-w*.7,y-.10},.37,.035,Steel);
        }
        HoloPart pelvis=Part(0,3.03,.0,1.05,0,-.7,.6,0);
        Plate(pelvis,new double[]{-.48,3.32,.48,3.32,.65,2.94,.24,2.73,0,2.86,-.24,2.73,-.65,2.94},.24,.5,Red);
        Plate(pelvis,new double[]{-.14,3.30,.14,3.30,.18,3.14,0,3.06,-.18,3.14},.32,.07,PaleGold);
        for(int side=-1;side<=1;side+=2)
        {
            double s=side;
            HoloPart thigh=Part(s*.43,2.48,0,1.25+(side+1)*.1,s*.85,-.55,.8,-s*.35);
            Plate(thigh,Mirror(new double[]{.17,2.89,.59,2.99,.71,2.68,.59,1.95,.26,1.98,.13,2.47},side),.21,.4,Gold);
            Plate(thigh,Mirror(new double[]{.42,2.88,.61,2.92,.64,2.60,.52,2.08,.35,2.03,.32,2.49},side),.29,.14,Red);
            Plate(thigh,Mirror(new double[]{.19,2.77,.32,2.78,.28,2.16,.19,2.09,.14,2.42},side),.27,.06,PaleGold);
            HoloPart knee=Part(s*.46,1.87,.09,1.65+(side+1)*.1,s*.7,-.35,1.2,-s*.3);
            Plate(knee,Mirror(new double[]{.25,2.03,.55,2.02,.67,1.84,.53,1.66,.28,1.70,.21,1.86},side),.35,.34,Scarlet);
            Plate(knee,Mirror(new double[]{.29,1.93,.51,1.97,.55,1.85,.39,1.78,.27,1.83},side),.42,.05,Steel);
            HoloPart shin=Part(s*.48,.96,0,1.8+(side+1)*.13,s*.9,-.2,1.15,-s*.3);
            Plate(shin,Mirror(new double[]{.26,1.72,.57,1.70,.69,1.42,.62,.36,.32,.34,.23,.65,.21,1.40},side),.24,.43,Red);
            Plate(shin,Mirror(new double[]{.32,1.66,.48,1.65,.54,1.29,.45,.47,.34,.42,.28,1.25},side),.36,.08,Scarlet);
            Plate(shin,Mirror(new double[]{.56,1.53,.64,1.44,.58,.55,.50,.45,.48,.85},side),.27,.06,Gold);
            Plate(shin,Mirror(new double[]{.29,1.43,.35,1.48,.32,.75,.27,.67},side),.30,.035,Steel);
            HoloPart boot=Part(s*.49,.20,.18,1.02+(side+1)*.12,s*.7,-.05,1.35,-s*.15);
            Plate(boot,Mirror(new double[]{.31,.41,.58,.42,.66,.25,.69,.05,.30,.05,.25,.17},side),.48,.68,Red);
            Plate(boot,Mirror(new double[]{.30,.15,.66,.15,.69,.05,.30,.05},side),.52,.06,Dark);
            HoloPart shoulder=Part(s*.95,4.84,-.02,2.25+(side+1)*.12,s*1.12,.35,.65,-s*.6);
            Plate(shoulder,Mirror(new double[]{.73,5.04,1.04,5.07,1.25,4.84,1.21,4.60,.97,4.52,.76,4.69},side),.17,.52,Red);
            Plate(shoulder,Mirror(new double[]{.80,5.01,1.03,5.02,1.18,4.82,1.15,4.72,1.00,4.72},side),.24,.05,Scarlet);
            HoloPart upperArm=Part(s*1.13,4.20,-.04,2.0+(side+1)*.1,s*1.2,.1,.85,-s*.55);
            Plate(upperArm,Mirror(new double[]{.99,4.60,1.20,4.63,1.36,4.16,1.28,3.88,1.04,3.93,.96,4.21},side),.12,.35,Gold);
            Plate(upperArm,Mirror(new double[]{1.16,4.52,1.24,4.45,1.35,4.13,1.26,3.97,1.17,4.02,1.12,4.27},side),.20,.065,PaleGold);
            HoloPart elbow=Part(s*1.19,3.84,0,2.7+(side+1)*.1,s*1.1,.05,1.0,-s*.4);
            Plate(elbow,Mirror(new double[]{1.04,3.99,1.28,3.99,1.35,3.78,1.15,3.66,1.01,3.82},side),.18,.37,Dark);
            HoloPart forearm=Part(s*1.26,3.47,.05,2.8+(side+1)*.12,s*1.1,-.1,1.1,-s*.55);
            Plate(forearm,Mirror(new double[]{1.10,3.85,1.34,3.85,1.48,3.53,1.40,3.18,1.17,3.16,1.05,3.50},side),.28,.39,Red);
            Plate(forearm,Mirror(new double[]{1.16,3.77,1.28,3.78,1.37,3.48,1.30,3.23,1.20,3.25,1.13,3.47},side),.36,.05,Scarlet);
            Plate(forearm,Mirror(new double[]{1.37,3.62,1.44,3.52,1.37,3.24,1.30,3.24},side),.31,.035,Gold);
            HoloPart hand=Part(s*1.27,2.99,.08,3.15+(side+1)*.1,s*.9,-.45,1.35,-s*.55);
            Plate(hand,Mirror(new double[]{1.18,3.18,1.38,3.18,1.43,2.94,1.35,2.79,1.19,2.81,1.12,3.0},side),.30,.27,Red);
            for(int finger=0;finger<4;finger++)
                Plate(hand,Mirror(new double[]{1.18+finger*.047,2.92,1.22+finger*.047,2.92,1.22+finger*.047,2.78,1.18+finger*.047,2.78},side),.32,.03,Scarlet);
            lights.Add(new HoloLight(hand,new HoloVector[]{V(s*1.225,3.08,.355),V(s*1.325,3.08,.355),V(s*1.325,2.98,.355),V(s*1.225,2.98,.355)}));
        }
        HoloPart neck=Part(0,5.25,0,3.25,0,.75,-.45,0);
        Plate(neck,new double[]{-.22,5.42,.22,5.42,.28,5.10,-.28,5.10},.12,.37,Dark);
        HoloPart helmet=Part(0,5.80,.04,3.5,0,.65,.7,.18);
        Plate(helmet,new double[]{-.24,6.26,.24,6.26,.43,6.06,.45,5.73,.32,5.40,.17,5.31,-.17,5.31,-.32,5.4,-.45,5.73,-.43,6.06},.29,.61,Red);
        Plate(helmet,new double[]{-.13,6.28,.13,6.28,.19,6.12,.11,5.99,-.11,5.99,-.19,6.12},.36,.08,Scarlet);
        HoloPart faceplate=Part(0,5.76,.39,4.15,0,.30,1.3,.15);
        faceplate.Duration=1.10;
        Plate(faceplate,new double[]{-.31,6.13,-.12,6.11,0,5.99,.12,6.11,.31,6.13,.36,5.84,.27,5.55,.13,5.42,-.13,5.42,-.27,5.55,-.36,5.84},.39,.12,Gold);
        Plate(faceplate,new double[]{-.23,5.71,0,5.80,.23,5.71,.18,5.53,0,5.47,-.18,5.53},.45,.04,PaleGold);
        Plate(faceplate,new double[]{-.17,5.48,.17,5.48,.11,5.43,-.11,5.43},.47,.03,Dark);
        // Eye apertures are actual polygons on the faceplate, not a light bar.
        HoloVector[] leftEye={V(-.31,5.87,.49),V(-.065,5.83,.49),V(-.085,5.76,.49),V(-.275,5.80,.49)};
        HoloVector[] rightEye={V(.065,5.83,.49),V(.31,5.87,.49),V(.275,5.80,.49),V(.085,5.76,.49)};
        lights.Add(new HoloLight(faceplate,leftEye)); lights.Add(new HoloLight(faceplate,rightEye));
    }

    private HoloVector Camera(HoloVector vertex, double time)
    {
        HoloVector centered=vertex-V(0,3.12-.025*Math.Sin(time*1.3),0);
        return HoloVector.Rotate(centered,-.28+.17*Math.Sin(time*.62),.025,0);
    }
    private PointF Project(HoloVector view, int width, int height)
    {
        double scale=height/8.9 * 9.0/(9.0-view.Z);
        return new PointF((float)(width*.5+view.X*scale),(float)(height*.52-view.Y*scale));
    }
    private PointF ScreenPoint(HoloVector vertex, double time, int width, int height)
    { return Project(Camera(vertex,time),width,height); }
    private static Color Tint(Color color, double light, double alpha)
    {
        return Color.FromArgb((int)(255*Clamp(alpha)),(int)Math.Min(255,color.R*light),
            (int)Math.Min(255,color.G*light),(int)Math.Min(255,color.B*light));
    }
    private static void Glow(Graphics g, PointF center, float rx, float ry, Color color, double opacity)
    {
        if(opacity<=.001 || rx<=0 || ry<=0) return;
        using(GraphicsPath path=new GraphicsPath())
        {
            path.AddEllipse(center.X-rx,center.Y-ry,rx*2,ry*2);
            using(PathGradientBrush brush=new PathGradientBrush(path))
            {
                brush.CenterColor=Color.FromArgb((int)(140*Clamp(opacity)),color);
                brush.SurroundColors=new Color[]{Color.FromArgb(0,color)};
                g.FillPath(brush,path);
            }
        }
    }

    internal Bitmap Render(double time, int width, int height, bool ready)
    {
        Bitmap bitmap=new Bitmap(width,height,PixelFormat.Format32bppPArgb);
        using(Graphics g=Graphics.FromImage(bitmap))
        {
            g.Clear(Color.Transparent); g.SmoothingMode=SmoothingMode.AntiAlias;
            g.CompositingQuality=CompositingQuality.HighQuality;
            double fade=Smooth(time/.65);
            DrawProjection(g,time,width,height,fade);
            List<HoloDrawFace> faces=new List<HoloDrawFace>();
            foreach(HoloPart part in parts)
            {
                double p=part.Progress(time);
                foreach(HoloFace face in part.Faces)
                {
                    PointF[] points=new PointF[3]; HoloVector[] moved=new HoloVector[3];
                    double depth=0,y=0;
                    double facet=Smooth((p-.08-(face.Seed%17)*.012)/.6);
                    for(int i=0;i<3;i++)
                    {
                        HoloVector v=part.Move(face.Vertices[i],time);
                        v.Z+=(1-facet)*.16*Math.Sin(face.Seed*2.31);
                        moved[i]=Camera(v,time); points[i]=Project(moved[i],width,height); depth+=moved[i].Z/3; y+=v.Y/3;
                    }
                    HoloVector normal=HoloVector.Cross(moved[1]-moved[0],moved[2]-moved[0]);
                    double len=Math.Max(.00001,normal.Length);
                    double light=.50+.60*Math.Abs((normal.X*-.48+normal.Y*.6+normal.Z*.64)/len);
                    double spec=Math.Pow(Math.Abs(normal.Z/len),12)*.23;
                    faces.Add(new HoloDrawFace { Points=points, Depth=depth, Color=face.Color,
                        Light=light+spec, Alpha=fade*(.02+.88*facet), Progress=p, Y=y, Seed=face.Seed });
                }
            }
            faces.Sort(delegate(HoloDrawFace a,HoloDrawFace b){return a.Depth.CompareTo(b.Depth);});
            foreach(HoloDrawFace face in faces)
            {
                if(face.Alpha>.025)
                    using(SolidBrush brush=new SolidBrush(Tint(face.Color,face.Light,face.Alpha))) g.FillPolygon(brush,face.Points);
                double wire=(.15+.5*(1-face.Progress))*fade;
                Color edge=face.Progress<.7?Color.FromArgb((int)(wire*120),93,221,249):Color.FromArgb((int)(wire*100),255,214,157);
                using(Pen pen=new Pen(edge,.55f)) g.DrawPolygon(pen,face.Points);
                double scan=(time*.73)%6.8;
                if(Math.Abs(face.Y-scan)<.045)
                    using(Pen pen=new Pen(Color.FromArgb((int)(100*fade),161,247,255),1)) g.DrawPolygon(pen,face.Points);
            }
            DrawParticles(g,time,width,height,fade);
            DrawLights(g,time,width,height,ready,fade);
        }
        return bitmap;
    }

    private void DrawProjection(Graphics g, double time, int width, int height, double fade)
    {
        PointF origin=ScreenPoint(V(0,-.17,0),time,width,height);
        Glow(g,origin,width*.16f,height*.025f,Color.FromArgb(40,211,247),fade*.12);
        using(Pen grid=new Pen(Color.FromArgb((int)(27*fade),78,217,239),.7f))
        {
            for(int n=-5;n<=5;n++)
            {
                g.DrawLine(grid,ScreenPoint(V(n*.28,-.18,-1.2),time,width,height),ScreenPoint(V(n*.28,-.18,1.2),time,width,height));
                g.DrawLine(grid,ScreenPoint(V(-1.4,-.18,n*.24),time,width,height),ScreenPoint(V(1.4,-.18,n*.24),time,width,height));
            }
        }
        // Faint volumetric beams rise from a tiny projector below the suit.
        PointF a=ScreenPoint(V(-1.15,5.8,-.45),time,width,height),b=ScreenPoint(V(1.15,5.8,-.45),time,width,height);
        using(LinearGradientBrush beam=new LinearGradientBrush(new PointF(origin.X,origin.Y),new PointF(origin.X,height*.10f),
            Color.FromArgb((int)(9*fade),53,223,255),Color.FromArgb(0,53,223,255)))
            g.FillPolygon(beam,new PointF[]{origin,a,b});
        using(Pen line=new Pen(Color.FromArgb((int)(105*fade),99,228,247),1.1f))
        {
            foreach(int side in new int[]{-1,1})
            {
                g.DrawLine(line,ScreenPoint(V(side*1.5,-.18,.8),time,width,height),ScreenPoint(V(side*1.2,-.18,.8),time,width,height));
                g.DrawLine(line,ScreenPoint(V(side*1.5,-.18,.8),time,width,height),ScreenPoint(V(side*1.5,-.18,.5),time,width,height));
            }
        }
    }

    private void DrawParticles(Graphics g,double time,int width,int height,double fade)
    {
        foreach(HoloPart part in parts)
        {
            double p=part.Progress(time);
            if(p>=1 || time<part.Start-.55) continue;
            for(int k=0;k<part.Faces.Count;k+=3)
            {
                HoloFace face=part.Faces[k]; HoloVector target=(face.Vertices[0]+face.Vertices[1]+face.Vertices[2])*(1.0/3);
                double phase=(time*1.3+face.Seed*.173)%1;
                double q=Smooth(phase), wave=(1-q)*.28;
                HoloVector source=V(target.X*.35,-.12,Math.Sin(face.Seed)*.6);
                HoloVector position=source*(1-q)+part.Move(target,time)*q;
                position.X+=wave*Math.Sin(q*8+face.Seed); position.Z+=wave*Math.Cos(q*7+face.Seed);
                PointF point=ScreenPoint(position,time,width,height);
                double alpha=fade*(1-p)*(.2+.8*q);
                Color color=face.Seed%4==0?Color.FromArgb((int)(210*alpha),255,195,100):Color.FromArgb((int)(180*alpha),99,232,255);
                float size=(float)(.8+q*1.3);
                using(SolidBrush brush=new SolidBrush(color)) g.FillRectangle(brush,point.X,point.Y,size,size*.55f);
            }
        }
    }

    private void DrawLights(Graphics g,double time,int width,int height,bool ready,double fade)
    {
        double energized=ready?Smooth((time-4.8)/.65):.18;
        double pulse=ready?.85+.15*Math.Sin(time*3.4):.35+.18*Math.Sin(time*2.4);
        foreach(HoloLight light in lights)
        {
            double p=light.Part.Progress(time);
            PointF[] polygon=new PointF[light.Polygon.Length];
            PointF center=new PointF();
            for(int i=0;i<polygon.Length;i++)
            {
                polygon[i]=ScreenPoint(light.Part.Move(light.Polygon[i],time),time,width,height);
                center.X+=polygon[i].X/polygon.Length; center.Y+=polygon[i].Y/polygon.Length;
            }
            double alpha=fade*Smooth((p-.72)/.28)*energized;
            float span=Math.Max(2,Math.Abs(polygon[1].X-polygon[0].X));
            Glow(g,center,span*1.05f,span*.65f,Color.FromArgb(105,226,255),alpha*.85);
            using(SolidBrush brush=new SolidBrush(Color.FromArgb((int)(255*Clamp(alpha*pulse)),218,251,255))) g.FillPolygon(brush,polygon);
        }
        HoloVector c=chest.Move(V(0,4.57,.515),time);
        PointF reactor=ScreenPoint(c,time,width,height);
        float radius=(float)(height/8.9*.12);
        double on=fade*Smooth((time-1.6)/.5);
        Glow(g,reactor,radius*3.6f,radius*3.6f,Color.FromArgb(66,223,255),on*(.4+.55*energized));
        using(SolidBrush rim=new SolidBrush(Color.FromArgb((int)(230*on),51,84,101))) g.FillEllipse(rim,reactor.X-radius*1.4f,reactor.Y-radius*1.4f,radius*2.8f,radius*2.8f);
        using(Pen border=new Pen(Color.FromArgb((int)(220*on),149,226,239),1.4f)) g.DrawEllipse(border,reactor.X-radius*1.2f,reactor.Y-radius*1.2f,radius*2.4f,radius*2.4f);
        using(SolidBrush core=new SolidBrush(Color.FromArgb((int)(255*on),205,249,255))) g.FillEllipse(core,reactor.X-radius,reactor.Y-radius,radius*2,radius*2);
    }

    internal static void RenderFrames(string directory)
    {
        string root=Path.GetFullPath(directory);
        Directory.CreateDirectory(root);
        HologramRenderer renderer=new HologramRenderer();
        double[] times={.6,1.7,2.9,4.25,5.75,6.8};
        for(int i=0;i<times.Length;i++)
        {
            string path=Path.Combine(root,"armor-"+i.ToString("00")+".png");
            if(File.Exists(path)) throw new IOException("Preview frame already exists");
            using(Bitmap image=renderer.Render(times[i],800,960,true))
            {
                image.Save(path,ImageFormat.Png);
                using(Bitmap review=new Bitmap(800,960,PixelFormat.Format32bppArgb))
                using(Graphics g=Graphics.FromImage(review))
                {
                    g.Clear(Color.FromArgb(11,19,29)); g.DrawImageUnscaled(image,0,0);
                    review.Save(Path.Combine(root,"review-"+i.ToString("00")+".png"),ImageFormat.Png);
                }
            }
        }
    }

    internal static void Check()
    {
        HologramRenderer renderer=new HologramRenderer();
        if(renderer.parts.Count<20 || renderer.lights.Count!=4) throw new InvalidOperationException("Incomplete armor");
        foreach(double t in new double[]{0,1,3,5.8,10})
            using(Bitmap bitmap=renderer.Render(t,360,440,true))
            {
                if(bitmap.GetPixel(0,0).A!=0 || bitmap.GetPixel(359,439).A!=0)
                    throw new InvalidOperationException("Preview is not transparent");
            }
    }
}

internal sealed class HologramStartupForm : Form
{
    [StructLayout(LayoutKind.Sequential)] private struct POINT { internal int X,Y; internal POINT(int x,int y){X=x;Y=y;} }
    [StructLayout(LayoutKind.Sequential)] private struct SIZE { internal int X,Y; internal SIZE(int x,int y){X=x;Y=y;} }
    [StructLayout(LayoutKind.Sequential,Pack=1)] private struct BLEND { internal byte Operation,Flags,Alpha,Format; }
    [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr h);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr h,IntPtr dc);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(IntPtr dc);
    [DllImport("gdi32.dll")] private static extern IntPtr SelectObject(IntPtr dc,IntPtr obj);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr obj);
    [DllImport("user32.dll",SetLastError=true)] private static extern bool UpdateLayeredWindow(IntPtr h,IntPtr screen,ref POINT location,ref SIZE size,IntPtr source,ref POINT origin,uint key,ref BLEND blend,uint flags);

    private readonly HologramRenderer renderer=new HologramRenderer();
    private readonly Stopwatch clock=new Stopwatch();
    private readonly Timer timer=new Timer();
    private readonly Func<bool> ready;
    private readonly Func<bool> stopped;
    private readonly bool hold;
    private double lastReadyCheck;
    private bool isReady;

    internal HologramStartupForm(Func<bool> readiness,bool holdPreview,Func<bool> stoppedListener=null)
    {
        ready=readiness; stopped=stoppedListener; hold=holdPreview; Text="Jarvis Holographic Startup";
        FormBorderStyle=FormBorderStyle.None; ShowInTaskbar=holdPreview; TopMost=true;
        StartPosition=FormStartPosition.Manual;
        Rectangle screen=Screen.PrimaryScreen.Bounds;
        int height=Math.Min(960,(int)(screen.Height*.79));
        ClientSize=new Size((int)(height*.84),height);
        Location=new Point(screen.Left+(screen.Width-Width)/2,screen.Top+(screen.Height-Height)/2);
        timer.Interval=33; timer.Tick+=Tick;
        KeyPreview=true;
        KeyDown+=delegate(object sender,KeyEventArgs e) { if(hold && e.KeyCode==Keys.Escape) Close(); };
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams
    {
        get { CreateParams cp=base.CreateParams; cp.ExStyle|=0x80000; if(!hold) cp.ExStyle|=0x80|0x08000000|0x20; return cp; }
    }
    protected override void OnShown(EventArgs e)
    { base.OnShown(e); clock.Start(); timer.Start(); Tick(this,EventArgs.Empty); }
    private void Tick(object sender,EventArgs e)
    {
        double seconds=clock.Elapsed.TotalSeconds;
        if(seconds-lastReadyCheck>.25)
        {
            lastReadyCheck=seconds;
            try
            {
                if(stopped!=null && stopped()) { Close(); return; }
                isReady=ready==null || ready();
            }
            catch { isReady=false; }
        }
        if((!hold && seconds>=7.8 && isReady) || seconds>(hold?120:24)) { Close(); return; }
        double t=hold?Math.Min(seconds,5.9):seconds;
        double opacity=(!hold && seconds>7.0 && isReady)?1-HologramRenderer.Clamp((seconds-7.0)/.8):1;
        using(Bitmap bitmap=renderer.Render(t,Width,Height,isReady)) Present(bitmap,(byte)(255*opacity));
    }
    private void Present(Bitmap bitmap,byte opacity)
    {
        IntPtr screen=GetDC(IntPtr.Zero),memory=IntPtr.Zero,pixels=IntPtr.Zero,previous=IntPtr.Zero;
        try
        {
            memory=CreateCompatibleDC(screen); pixels=bitmap.GetHbitmap(Color.FromArgb(0)); previous=SelectObject(memory,pixels);
            POINT location=new POINT(Left,Top),origin=new POINT(0,0); SIZE size=new SIZE(Width,Height);
            BLEND blend=new BLEND { Operation=0,Flags=0,Alpha=opacity,Format=1 };
            if(!UpdateLayeredWindow(Handle,screen,ref location,ref size,memory,ref origin,0,ref blend,2))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
        finally
        {
            if(previous!=IntPtr.Zero) SelectObject(memory,previous);
            if(pixels!=IntPtr.Zero) DeleteObject(pixels);
            if(memory!=IntPtr.Zero) DeleteDC(memory);
            if(screen!=IntPtr.Zero) ReleaseDC(IntPtr.Zero,screen);
        }
    }
    protected override void Dispose(bool disposing)
    { if(disposing) { timer.Stop(); timer.Dispose(); } base.Dispose(disposing); }
}
